# 本地部署与恢复验证

生产部署与客户 SSO 尚未验收。以下是独立测试数据库的开发验证入口，不是生产安全配置。

```powershell
python -m venv --system-site-packages medic/.venv
medic/.venv/Scripts/python.exe -m pip install -r medic/requirements-postgres.txt
$env:MEDIC_TEST_POSTGRES_DSN = 'postgresql://<test-user>@127.0.0.1:<test-port>/<database>_test'
medic/.venv/Scripts/python.exe -m unittest medic.tests.test_postgres -v
```

`Store` 支持 PostgreSQL DSN。数据库事务以 run ID 为粒度取得事务级 advisory lock，保持任务图、租约、outbox、结果和预算原子；不同 run 不共享这把锁。模型执行在事务之外。当前方案没有声称使用 `SKIP LOCKED`；跨 run 吞吐量、组织权限和生产连接池仍需部署验收。

共享业务 SQL 仅在后端适配层转换本项目使用的参数占位符、幂等插入、顺序字段和 JSON epoch 查询；业务值始终通过绑定参数传递。PostgreSQL schema 另存为 `runtime/postgres.sql`，有外键与查询索引。SQLite 本地与 PostgreSQL 运行同一组语义契约测试。

Windows 测试二进制来源：[PostgreSQL 官方 Windows 下载页](https://www.postgresql.org/download/windows/) 指向的 [EDB 二进制包](https://www.enterprisedb.com/download-postgresql-binaries?lang=en)。本地包、数据目录和日志存于忽略的 `medic/artifacts/postgres/`，未安装系统服务。测试实例只监听 loopback，不能把测试认证方式照搬到生产。

## 实际中断与恢复探针

`python -m medic.evals.postgres_faults <stage> --checkpoint medic/artifacts/<new-probe>/checkpoint.json` 仅允许明确的 loopback `*_test` 数据库。探针不启停服务：操作者负责只对独立测试实例注入故障。

1. `prepare` 创建合成任务、领取租约并预留 1,000 tokens，保存私有 checkpoint。
2. 单独进程运行 `hold`，等待 `.ready.json` 后，针对该测试数据目录执行 PostgreSQL `immediate` 停止。
3. `unavailable` 核验数据库访问失败，不能自动切换 SQLite。
4. 重启同一数据目录，然后 `verify --result <summary.json>`：核验 postmaster 确实重启、活动连接断开、未提交写入回滚、任务与预算保留；仅将该合成租约设为过期，重派为 epoch 2，拒绝旧凭据，最后取消测试 run。

checkpoint 包含租约令牌，只能放在被忽略的 artifacts 中。重新演练须选新目录。验收摘要见 [p2-postgres-crash-verification.json](../evals/results/p2-postgres-crash-verification.json)。此演练不代表生产高可用、备份恢复或客户部署已验收。

## SQLite 应用与材料备份

`medic.deploy.backup` 提供本地 SQLite 应用状态的备份、验证和恢复。它使用 SQLite 在线 backup 接口捕获已提交 WAL 数据，先固定运行数据库，再固定只追加的材料快照数据库，最后复制快照引用的不可变原件。逐项验证数据库完整性、快照 hash、员工/周期归属、评估和补证引用，以及原件大小与 SHA-256。上传可继续进行；运行数据库快照之后新增的材料快照可能被一并保留，但不会凭空出现在较早的应用记录中。此一致性依赖当前材料库只追加；未来引入删除/留存清理时须重新设计协调机制。

```powershell
medic/.venv/Scripts/python.exe -m medic.deploy.backup create --database medic/var/local-api/runtime.sqlite3 --vault medic/var/local-api/vault --output medic/artifacts/backups/new-snapshot
medic/.venv/Scripts/python.exe -m medic.deploy.backup verify medic/artifacts/backups/new-snapshot
medic/.venv/Scripts/python.exe -m medic.deploy.backup restore medic/artifacts/backups/new-snapshot --output medic/var/new-restored-api
```

输出必须是 `medic/artifacts`、`medic/private` 或 `medic/var` 下的全新目录。既有备份或部署不会被覆盖；拒绝路径穿越、符号链接/junction、不在清单内的文件，以及材料库内的备份目录或备份内的恢复目录。只有完整复制和核验成功后才写入 `manifest.json`；未完成目录不能通过验证。清单 hash 用于完整性检查，不是供应链签名、加密或离线存储策略。

恢复要求与捕获时相同的冻结代码和执行环境，成功后生成 `RESTORED.json`，不会启动 API 或调度服务。运行、预算、截止时间、租约、原 AI 报告、人工历史和授权记录保持备份时状态；所有复制的登录会话清除，原部署会话不变。正式接管前应停用原调度和写入口，核对恢复时点及当前组织授权，再通过现有运维配置接口签发新会话；不要同时运行两个恢复自同一预算的评估副本。已过去的截止时间不会因恢复获得新预算。

备份包含私有原件和业务数据库，不包含 `local-access.json`、provider 凭据、发布源码包以及外部 worker 协议/trace 归档。独立终态 SQLite 运行的协议与发布源码归档见文末工具；应用数据库、材料库及多运行/多后端协议的统一恢复编排、异机恢复和客户 RPO/RTO 仍待实施或验收。SQLite 工具明确拒绝 PostgreSQL DSN；后者使用下节的原生工具。

### 已执行的恢复演练

13 项备份契约测试通过，覆盖 WAL、备份期间上传、有效任务和未结算预算、损坏原件/数据库、跨库引用、路径越界、目录覆盖、构建身份以及会话失效。[测试摘要](../evals/results/p5-backup-contracts-verification.json)保存工具、测试源码和日志 hash。

使用既有真实六角色运行 `run-f7aabe00b502f884b00b717831eac17c` 的合成材料与合成人工定稿，备份后恢复到独立目录并启动临时真实 HTTP 服务，**15 项核验通过**：除会话外所有数据表内容一致，原件下载及全部证据定位正确，AI 报告/最终结果/复核历史保留，原权限有效、无授权上级被拒绝、员工不能自审，三个旧会话均失效。未新增模型请求，原数据保持不变。见[恢复与 HTTP 核验](../evals/results/p5-backup-restore-verification.json)。

首轮演练脚本误读了证据定位接口的嵌套返回结构，在核验中停止；修正后使用新目录完整重跑，并在增加目录重叠防护后再次重跑。失败目录保留，不计为通过。当前小样本备份约 0.077 秒、恢复约 0.056 秒，仅是本机测量，不作为生产容量或恢复时间承诺。临时 API 已停止，当前页面服务未被重启。

## PostgreSQL 原生备份与恢复

`medic.deploy.postgres_backup` 使用 `pg_export_snapshot()` 导出的只读重复读快照：表内容 hash 和 `pg_dump --snapshot` 对齐同一个应用数据时间点，再捕获只追加材料库。备份为 PostgreSQL custom archive、材料快照 SQLite 和原件，原生 `pg_restore --list` 检查归档可读；恢复后重新计算全部表内容 hash，并检查跨库引用。没有将 PostgreSQL 数据转成 SQLite 作为运行库。

```powershell
# 两个环境变量分别保存源数据库 URI 和维护数据库 URI（dbname 为 postgres）。
# URI 来自本地运维配置，不把带密码的连接串写进命令行。
medic/.venv/Scripts/python.exe -m medic.deploy.postgres_backup --bin-dir medic/artifacts/postgres/pgsql/bin create --database-env MEDIC_PG_SOURCE --vault medic/var/pg-api/vault --output medic/artifacts/backups/new-pg-snapshot
medic/.venv/Scripts/python.exe -m medic.deploy.postgres_backup --bin-dir medic/artifacts/postgres/pgsql/bin verify medic/artifacts/backups/new-pg-snapshot
medic/.venv/Scripts/python.exe -m medic.deploy.postgres_backup --bin-dir medic/artifacts/postgres/pgsql/bin restore medic/artifacts/backups/new-pg-snapshot --maintenance-env MEDIC_PG_MAINT --database-name medic_restore_new_trial --output medic/var/new-pg-restored
```

恢复只允许创建尚不存在的 `medic_restore_...` 数据库，要求维护账号具备 CREATE DATABASE 权限；不使用 `--clean`，不替换旧库。新库撤销 PUBLIC CONNECT，只移除 template0 自带的空 public schema（不使用 CASCADE），再以单事务还原归档。所有表内容核验通过后清除备份会话，保留应用用户/组织授权、历史、任务和预算；不会启动服务。任何阶段失败都不写成功标记，失败的新数据库与目录留待检查，不自动删除或反复覆盖。

当前覆盖 public schema 中的 MEDIC 应用表；非 public 表、large objects 和未支持的连接选项明确拒绝，避免静默漏备份。数据库角色、数据库/对象 ACL、表空间、provider 凭据、发布包与外部协议归档不在本工具恢复范围；数据库所有权归执行恢复的账号，生产服务账户及连接授权需单独配置。保持同一冻结应用环境，并沿用上一节的接管顺序与授权核对要求。

本地 PostgreSQL **17.11** 原生验证已通过：**11 项真实数据库测试**覆盖导出快照期间的并发上传、有效租约/未结算预算/截止时间保留、序列续增、历史/权限/原件恢复、坏归档拒收、不覆盖既有库及新库 CONNECT 权限。见[原生备份契约摘要](../evals/results/p5-postgres-backup-contracts-verification.json)。原生二进制和工具源码 hash 均已记录。

另在恢复库上启动独立 loopback HTTP 服务，**13 项核验通过**，包括完整员工结果/复核历史、原件下载与证据定位、旧令牌失效、上级授权及无授权访问拒绝、原库不变和临时数据库清理。见[原生恢复 HTTP 摘要](../evals/results/p5-postgres-restore-verification.json)。本次报告明确为合成 fixture，原 AI gate 为失败且恢复后仍失败，模型请求为 0；不冒充真实模型质量验证。样本备份约 0.260 秒、恢复约 0.317 秒，不作为生产容量或 RPO/RTO 承诺。

初轮验证发现两类问题：测试夹具误用关键字连接串，未进入 PostgreSQL；新建数据库的默认 public schema 与归档中的创建语句冲突。已修正 URI 夹具并增加实际后端断言，恢复仅处理自己新建库的空 schema。另修正了并发测试钩子对 `pg_dump --version` 的误触发。失败日志及误生成的合成文件留在私有目录，未计入通过。

上述验证发现的应用入口问题已在新发布 `medic-runtime-p3-v6` 修复：关键字连接串和不支持的 URI 在文件操作前被拒绝；API 新增 `--database-env`，连接失败不会创建 SQLite 替代库，也不回显连接内容。应用连接 PostgreSQL 使用 `postgresql://...` 或 `postgres://...` URI。真实 PostgreSQL 入口/重启核验见 [P3 记录](../P3_PROGRESS.md)；v5 的旧备份保持原身份，恢复仍要求其对应的冻结环境。

## 终态运行的协议归档与离线恢复

`medic.deploy.terminal_archive` 保存单个已结束、独立 SQLite runtime 运行的完整数据库、`run.json`、网关用量回执、每次实际租约的 worker 协议摘要和原始 JSONL 会话，以及对应发布清单与源码包。成功、失败和技术重试的已启动 worker 都必须有记录，不能仅保留成功角色。取消增加的隔离 epoch 不被当成新执行；以真实 `task_leased` 事件确定尝试集合。缺失协议或已绑定线程缺失会话时拒绝生成成功清单。

```powershell
medic/.venv/Scripts/python.exe -m medic.deploy.terminal_archive create --folder medic/artifacts/<terminal-run> --release-manifest medic/deploy/releases/<version>.manifest.json --release-archive medic/artifacts/releases/<version>/source.zip --output medic/artifacts/<new-archive>
medic/.venv/Scripts/python.exe -m medic.deploy.terminal_archive verify medic/artifacts/<new-archive>
medic/.venv/Scripts/python.exe -m medic.deploy.terminal_archive restore medic/artifacts/<new-archive> --output medic/artifacts/<new-restored-evidence>
```

输出必须位于 `medic/artifacts`、`medic/private` 或 `medic/var` 的新目录，拒绝目录重叠、路径穿越、符号链接/junction、额外成员与覆盖。数据库通过 SQLite backup 接口捕获已提交 WAL；复制前后核对全部 14 张表，并检查任务上下文、读取记录、历史结果、mailbox、返工计划、快照和用量账本一致。逐文件 hash、字节数、尝试覆盖及未知实际用量写入完整性清单；未知用量不会被保守计账替代。工作目录中的 binding、Codex 配置/auth、provider 凭据不会复制，重新计算清单也不能允许这些文件成为合法成员。

恢复用于离线审计，不启动 API、调度或模型，不签发会话，不改变失败状态、截止时间或预算。归档仍含私有原文和模型文本，只能按私有材料管理。它不包含应用身份/材料库、多运行数据库、PostgreSQL、原始 Office/PDF、任意日志或实验驱动脚本；这些继续使用对应备份与独立制品管理。该工具不替代旧版本环境与依赖、供应链签名、加密、异机存储、生产接管或客户留存策略。

最终 **28 项检查通过**（15 项新增归档契约与 13 项既有 SQLite 备份回归），无跳过。首轮将取消后的隔离 epoch 错当成执行租约而拒绝合法案例，已按实际租约事件修正并保留失败日志。真实 v16 完整评估失败案例另完成本机归档和新目录恢复：**42 文件、18 个 worker 尝试与会话、14 张表一致**，15 个已完成角色的原协议审计结果一致，原失败状态与未知用量保留，未新增模型调用或启动服务。见[终态协议恢复核验](../evals/results/p5-terminal-archive-verification.json)。该工具源码和测试独立归档，没有修改运行应用 v16 的 106 文件清单。
