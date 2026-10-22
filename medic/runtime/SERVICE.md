# 本地持续队列调度

`medic.runtime.service` 持续发现数据库中新建的合成评估，调用原 runtime 执行真实角色。API 只创建持久化任务，服务不创建替代 run、不制造报告、不在失败时切换演示数据。当前服务限定 Windows；SQLite 和 PostgreSQL 均支持，生产服务安装、高可用及跨主机压测尚未验收。

## 启动与停止

先按 [API 说明](../apps/api/README.md) 初始化新的私有合成目录并启动 API。在仓库根目录，以相同 Python 环境、同一冻结发布和同一数据库启动调度服务：

```powershell
$env:MEDIC_API_DB = 'C:/MEDIC-Competence-CodexCLI/medic/var/local-api/runtime.sqlite3'
medic/.venv/Scripts/python.exe -m medic.runtime.service --database-env MEDIC_API_DB --binary codex-rs/target/debug/codex.exe --api-example '<本地凭据文件>' --output medic/var/local-api/queue
```

PostgreSQL 使用同一个环境变量承载 DSN，不写入命令行或公开配置。服务先检查发布清单、provider 准入身份和 Codex 二进制 hash。只有这些检查与数据库互斥锁均通过后才打印 `service_ready`。模型凭据只从本地文件读取，输出必须位于 `medic/artifacts`、`medic/private` 或 `medic/var`。

服务默认每 2 秒查询一次，每个数据库同时处理一个评估；评估内部仍按原有预算执行 1–4 个并发任务。第二个调度服务立即退出。`--poll-seconds` 可设为 0.1–60；`--once` 仅执行一次发现、至多处理一个评估，用于运维检查，常驻服务不要加该参数。不要另外启动手工 runtime 来模拟服务吞吐量。

Ctrl+C 停止服务并终止本次评估进程树。再次启动相同版本后，服务发现未完成的运行；有效任务租约存在时先处理其他评估，待原租约到期后再恢复。旧租约通常最多剩余 480 秒，恢复不延长评估截止时间。已完成节点和已发布报告不会重跑。

## 互斥、恢复与失败

SQLite 使用数据库旁的 OS 字节锁；不要删除正在使用的 `.supervisor.lock` 文件。PostgreSQL 使用专用连接持有数据库级 advisory lock，连接断开时本服务退出，不偷偷重新连接后继续冒充原锁所有者。预算预留仍必须在数据库事务内成功，数据库不可用时不发送新模型请求。该机制不是已验收的多机容灾方案。

每次启动评估前，在 `service_attempts` 和 runtime 事件中记录 attempt。服务重启把遗留的 running 标记为 interrupted，但不改变任务租约、结果、预算或截止时间。一个 run 最多自动启动三次服务 attempt，与任务自己的三次技术 attempt 分开计数。达到上限后需运维检查，不能循环重试耗费模型额度。

已失败、取消或带报告的运行不由服务自动复活。执行版本不匹配的运行标记 blocked，不改写旧发布身份；未知子进程失败且未产生终态也标记 blocked。检查 `service_attempts`、`service_attempt_started/finished` 事件和私有 `process.log` 后再决定显式恢复。服务不会自动提高预算或重新创建一份评估。

每次执行独立写入 `<output>/<run_id>/attempt-N-随机后缀/`，包含 worker 协议、网关用量、run.json 和审计视图。启动、停止和退出状态为简短控制台事件；详细日志留在私有目录，不应公开上传。

Windows Job Object 由调度父进程持有。实际解释器在收到开始消息前自行加入该 Job，覆盖 venv 启动器可能提前派生解释器的情况；父进程退出或被终止时，Job 关闭会终止整个后代树。它用于进程生命周期管理，不替代生产文件、网络和服务账户隔离。

## 持续事务与进程故障探针

`medic.evals.runtime_load` 使用真实 Store、预算账本、Windows 子进程和数据库事务，持续创建合成任务。默认 300 秒、每 2 秒一项、4 个竞争进程和 1 个只读观察进程。每个任务只领取初始 Coordinator 租约，等待 3 秒自然到期，最多领取三次；达到技术尝试上限后取消。不会提交角色结果、启动 Codex / 模型请求或发布评分报告。账本中的 100 单位预留是显式模拟记账，不是实际 tokens。

```powershell
# 输出目录必须是尚不存在的私有目录；不要传入当前试用库。
medic/.venv/Scripts/python.exe -m medic.evals.runtime_load run --folder medic/artifacts/p2/new-transaction-load --duration 300 --interval 2 --lease 3

# 在已有 PostgreSQL 测试连接中创建独立新 schema，保留用于复核。
# 连接只从已配置的环境变量读取，不写入命令行或报告。
medic/.venv/Scripts/python.exe -m medic.evals.runtime_load run --folder medic/artifacts/p2/new-postgres-transaction-load --postgres-env MEDIC_TEST_POSTGRES_DSN
```

探针在持续到达期间，确认某进程持有有效租约和未结算预留，再关闭它的独立 Job，并用已打开的实际解释器句柄核查退出。替代进程重放尚未送达的旧回调；旧授权和完成请求必须被拒绝，旧失败回调不得改变新 attempt。中断预留保持未结算并继续占用总预算，不伪造模型返回或增加额度。父进程只通过 `recover` / `cancel` 操作任务，不直接修改租约、截止时间或结果。

目录保留启动前源码、输入、配置、创建时身份、逐进程操作日志、故障记录、数据库观测及核验报告。`fault-ready.json` 内含合成测试租约凭据，仅留在私有目录；不得将整个目录作为公开验证摘要发布。PostgreSQL schema 名在 `database-location.json` 中，连接凭据不落盘。核验器的反例测试覆盖重复领取、提前重排、预算丢失、截止时间重置、伪造报告、进程未死亡和回调覆盖缺失。

这是本地有限时长的事务竞争与进程故障验证。它只轮询 `Queue.pending()`，不执行生产 `serve()`、完整任务图、网关或模型；延迟仅属于指定操作，不能用作模型评分耗时或生产服务吞吐量。客户容量、长期稳定性、完整协议负载和上线验收仍需另行验证。

## 验证边界

`test_service.py` 在 SQLite 和真实 PostgreSQL 验证单实例竞争、连接丢失、有效/过期租约、旧版本隔离、截止时间、有限恢复以及预算不重置；另用真实 Windows 子进程验证无启动消息不执行、关闭 Job 后解释器及后代退出。HTTP 自动队列探针入口：

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.api_probe --queue-service --output medic/artifacts/p3/new-service-probe --binary codex-rs/target/debug/codex.exe --api-example '<本地凭据文件>'
```

探针只使用虚构 DOCX 和测试身份，先启动空队列服务，再经 HTTP 创建评估，检查第二服务被拒绝、自动执行、材料绑定和合成人工定稿。它不代表客户评分质量、业务签收或生产部署验收。

强制中止与续接探针使用独立的新 SQLite 数据库，不操作正在使用的页面实例：

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.service_crash_probe --output medic/artifacts/p3/new-service-crash-probe --binary codex-rs/target/debug/codex.exe --api-example '<本地凭据文件>'
```

探针先等待真实 Coordinator 完成、下游模型请求进入持久预算，然后强制终止实际持有 Job 的调度解释器。它使用已打开的 Windows 进程句柄核查观察到的后代退出，重新启动服务并验证有效租约期间不重派、预算和结果不变。之后仅对新建合成 run 提前到期任务租约与模型并发占位，保留原截止时间和所有请求记账；这不验证默认 480 秒等待时长，也不等同生产灾备。

恢复后重新校验六角色的实际协议、构建身份、硬规则、旧租约隔离和唯一发布。`audit/` 只复制各当前 task/epoch 的原始协议摘要，并保存来源及 hash；两个执行目录和未完成旧 attempt 保持原状。第二次执行的网关 trace 不能代表第一次的请求细节，累计用量以持久预算为准；被中断、无法结算的请求继续按预留估算计费，不清零或伪造为已结算。
