# P2 持久 runtime 进展

更新：2026-10-17。**部分完成，尚未通过 P2 全阶段验收。** P0/P1 的验收结论保持不变。

## 已实现及本地故障证据

- 持久预算账本：请求发送前原子预留请求数、token 和并发占用；重建网关、重启进程不清零。子进程以 `os._exit(23)` 退出后，预留额度仍存在；未知用量保守计入，不能被重试抹除。
- 共享 outbox 调度：领取消息与创建租约同事务提交；四个独立进程竞争时，只领取两个满足依赖的节点。过期重派递增 epoch，旧 attempt 的结果不能推进新任务。Coordinator 规划完成前子任务不能启动。
- 整次运行截止时间：进入模型请求、领取/使用租约及发布结果均校验截止时间；过期运行持久化为 failed。worker 定期检查权威状态并中断回合。
- 数据库不可用的注入测试：网关预留失败时返回失败，未向上游发出模型请求。这是客户端故障注入，不冒充 PostgreSQL 实际断连演练。
- 多材料/多维度单元图：逐组合检查完整五角色链、批量幂等派发、拒绝跨组合依赖和原文读取；human_only 建立人工待办并禁止 AI 评分任务。
- 发布仍在事务内重验实际结果；运行器会等待本进程 worker 收尾后再导出终态，避免保存遗漏晚到事件的旧快照。

## 真实模型恢复回归

运行：`run-ec61e8e8927540968edec8589dd6dbad`，私有记录在 `medic/artifacts/p2/live-01/`，公开校验见 [p2-recovery-verification.json](evals/results/p2-recovery-verification.json)。

第一次 Coordinator 已提交计划，但在 JSON 回执前附加说明文字，适配器拒收，run 记录为 failed；账本已记录 7 次请求、40,767 tokens。将这种独立尾段回执纳入格式兼容后，使用同一个 run 恢复：Coordinator 为 attempt 2，既有子任务按 ID 幂等保留，没有重复建图。之后六个角色均完成，Judge 和当前硬规则重验通过，报告为合成的 `ready_for_human_review`，不构成员工最终评价。

运行累计 **35 次模型请求、263,804 tokens**，包含第一次失败尝试；最终无未结算预留。没有重建 run、增加预算或改为 mock。旧 attempt 的提交结果继续保留在账本中。

## 真实多材料规划与回归测试

使用固定 `medic-p2-roles-v3` 指令和两份材料、两个维度的合成夹具，真实 Coordinator 在约 93 秒内建立 21 个节点：自身加 20 个子任务。逐节点重验全部五角色覆盖、唯一性和同材料同维度依赖均通过；所有子任务保持 queued，未发布评估报告。公开校验见 [p2-multi-plan-verification.json](evals/results/p2-multi-plan-verification.json)。这是 `--plan-only` 建图验收，不是完整多材料评估通过。

上述历史批次的本地回归为 67 项。随后配置 `MEDIC_TEST_POSTGRES_DSN` 的回归 **107 项通过**，其中 33 项在真实 PostgreSQL 上执行，包含失败回调 run/token/epoch 隔离、版本及端点准入检查。该历史批次摘要见 [p2-contracts-verification.json](evals/results/p2-contracts-verification.json)，新批次结果列在后文。真实执行证据单独记录，未把单元测试中的模拟 worker 当成真实模型。

## 跨材料汇总与 PostgreSQL

当前 v4 指令要求两材料×两维度共 31 个节点：Coordinator、20 个材料级角色、10 个汇总级角色。每个维度的 Aggregator 依赖所有材料的二评和 Judge；随后执行汇总反驳、二评和 Judge，汇总盲审可提前独立读取原文，不能读取已有评分。结果绑定来源 hash 与服务端计算的事件分组，重复材料不能增加独立佐证数量。当前门禁核验完整图及实际结果，不再使用旧的 blanket 汇总未实现阻断。

PostgreSQL 17.11 后端已实现任务、租约、outbox、结果、事件和持久预算；同 run 的短事务用 advisory lock 串行，模型执行不持有事务。已复用运行时、汇总、独立进程争抢和跨进程预算契约测试。

实际中断恢复通过，见 [p2-postgres-crash-verification.json](evals/results/p2-postgres-crash-verification.json)：在活动事务未提交时对独立测试实例执行 immediate 停止，确认客户端断连及数据库不可用；重启后已提交任务和 1,000 token 预留保留，未提交事件回滚。仅人工提前到期合成租约后重派为 epoch 2，旧租约被拒绝，未发布报告。测试实例只监听本机，未安装系统服务。这不是生产高可用或备份恢复验收。

PostgreSQL 真实单维度闭环已通过：run `run-b5f3b18908e74642accfcc89064aa255`，六角色均为独立真实 Codex/DeepSeek 执行，首评/二评均 B，Judge 与硬规则通过。累计 28 次请求、226,472 tokens，记录见 [p2-postgres-live-verification.json](evals/results/p2-postgres-live-verification.json)。独立复验确认当前 attempt 完成事件、角色线程、实际 prompt hash 和报告一致。测试数据库已在验证后停止，保留数据目录供复跑。

多材料 31 节点真实闭环已通过：run `run-1add0e890ed948339d206bd39ec459c3`，独立复验见 [p2-multi-live-verification.json](evals/results/p2-multi-live-verification.json)。两份材料、两个维度的材料级与汇总级首评/反驳/二评/Judge 链全部完成，M2、D1 汇总建议均为 B。每项汇总都绑定两个来源结果、一个来源事件，保留 self_reported 归属，未把重复材料计为独立佐证。

运行累计 **170 次请求、1,739,234 tokens**，其中 1 次中断请求按保守估算计入，未结算请求为 0；从创建至发布约 2,028 秒，包含排查与恢复时间，不作为吞吐量承诺。31 个节点均有独立线程、对应当前 attempt 的完成事件、正确 prompt hash 和读证据记录。最终硬规则与两个汇总 Judge 均通过，状态为 `ready_for_human_review`，仅是合成预评估包。

本次真实运行在 24 个节点完成后发现网关交接缺陷：SSE 已传出终态，但连接尚未 EOF 时仍占用并发槽，后续模型步骤收到本地 429。使用延迟 EOF 的合成传输复现后，改为收到终态即关闭上游流，并最多等待 2 秒交接槽位；活动流仍受原并发上限约束。修复后网关/持久预算 14 项及 P0 协议 18 项通过。已在同一 run 恢复两个失败节点，24 个完成节点保留，预算未重置、prompt 未改变。恢复前已使用 134 次请求、1,344,206 tokens（含被中断请求的保守估算）；记录见 [网关回归摘要](evals/results/p2-gateway-handoff-regression.json)。

随后 D1 汇总 Judge 在 attempt 1 只宣称准备提交而未调用提交工具，runtime 拒收且没有报告；仅重试该 Judge，attempt 2 完成真实提交和回执。其余 30 个完成节点保留。最终仅三个任务使用 attempt 2，未提高请求、token、并发或墙钟预算；原失败事件、旧 attempt 结果与各次网关记录保留在私有目录。缺少回执现返回明确错误代码，回执格式相关 3 项测试通过。这些是技术重试，不是已实现第三轮业务复评。

## Mailbox 与第三轮返工

v5 已实现受限争议消息、一次 Coordinator 返工决策、同维度下游失效、旧结果与上下文归档。第三轮必须显式回应上一轮未解决的 blocking challenge；即使新 Challenger 漏列旧问题，也不能省略。未答问题、旧争议未解决或 Coordinator 指定人工处理，均不能被 Judge `pass` 覆盖。业务最多三轮、每业务轮最多三个技术 attempt，两个计数独立，总预算不清零。细节见 [争议协议](runtime/DISPUTES.md)。

包含 PostgreSQL 的完整回归 **135 项通过，其中 46 项使用真实 PostgreSQL**，并追加核验两种数据库中的历史结果正文读取。覆盖单材料问题只失效该范围及同维度汇总下游、其他维度结果不变、无第四轮、消息越权/重放与结果不可覆盖。摘要见 [p2-disputes-contracts-verification.json](evals/results/p2-disputes-contracts-verification.json)。此前多材料 31 节点和 PostgreSQL 六角色真实记录也用当前门禁复验通过。

真实续接探针 `run-5231dc30a4a84c9685b4c095efdfdc8d` 已完成，[独立校验全部通过](evals/results/p2-rework-live-verification.json)。Coordinator、Challenger、第三轮 Re-evaluator 和 Judge 均使用独立真实线程；Coordinator 选择定向返工，Challenger 实际回答 mailbox 问题，第三轮明确回应了旧 blocking challenge。第三轮建议 B，Judge 为 pass，共 26 次请求、313,643 tokens。初始首评与盲审明确标记为 fixture，完整 live gate 正确阻断这两个节点，状态为 `awaiting_human_review`。该探针验证争议协议，不构成初始阶段也来自模型的完整评分验收。

探针还复现了“读取后只说准备提交”的模型行为。worker 增加最多两次原线程完成补正，与结构拒绝共享限制；不延长租约，不清零工具次数或预算。只有真实提交、正确回执和成功 turn 才能完成。首次失败 attempt 保留，后续实际使用一次补正完成 Challenger，未更换冻结 prompt。

## 初始六角色均为真实模型的争议案例

v7 增加两份前瞻登记的合成案例，登记后使用正常角色指令和固定预算执行，不构造角色结果。第一份 `measure-version-attribution`（run `run-e83f82c19f7f49eb98fe6a813da09462`）已完成：Challenger 对首评的同一主张提出 1 个 blocking 和 1 个 advisory 问题，二评分别接受和拒绝，并引用原文说明。

实际变化是 B1 从 `met` 改为 `partial`，A1 保持 `partial`；首评和二评均因证据不足保留空档位。Judge 为 `pass`，但程序门禁仍以 `requires_human_review:M2` 阻断自动放行，状态为 `awaiting_human_review`。初始六角色均完成真实独立线程和合法提交，只有一次预评估发布；40 次模型请求、453,970 tokens、0 笔未结算、无网关并发拒绝。盲审有 2 次结构拒绝、首评有 1 次结构拒绝；首评和 Challenger 各使用 1 次原线程补正。这些事件原样保留，没有手工替换结果或重置预算。

[16 项独立执行检查通过](evals/results/p2-dispute-measure-v7-verification.json)，覆盖输入、构建、预算、完整初始角色、每个完成 epoch 的协议、结果引用和人工出口。该案例没有消息往返和第三轮，因此整体争议覆盖状态是 `partial`，不冒充完整第三轮验收。

第二份 `denominator-and-selection`（run `run-ccff96381cff42549d639332544512a6`）也完成全部真实六角色，并通过[同样 16 项独立执行核验](evals/results/p2-dispute-denominator-v7-verification.json)。Challenger 提出 3 个 advisory 问题，二评全部接受并修订表述与归属边界，首评/二评均为 B，Judge 与硬门禁通过，进入 `ready_for_human_review`。37 次请求、433,429 tokens，全部结算，无网关并发拒绝。盲审与 Challenger 各有 1 次结构拒绝，Challenger 使用 1 次补正、二评使用 2 次补正；二评耗时约 391 秒但保持原 480 秒租约，未重新领取或延长截止时间。

[完整套件记录](evals/results/p2-dispute-suite-v7-verification.json) 保留 2/2 个预登记案例，合计 **77 次请求、887,399 tokens、5 个真实争议及 5 个正式回应**。12 个角色任务均在首次 lease epoch 完成，同时明确记录 5 次原线程补正和 5 次被拒提交，不能把首次任务 attempt 误写为无补正。两个案例均无 mailbox 和第三轮，套件覆盖仍为 `partial`。这些数据表明当前完成协议在复杂材料上有补正与耗时开销；角色耗时、实际请求量和预算留作后续效率/负载验证依据，不宣称专家评分质量或多 Agent 总体收益已获证实。

新核验器同时检查当前与历史轮次，防止第三轮成功掩盖初始 fixture 或缺失协议；11 项合成契约测试通过，见[核验器测试记录](evals/results/p2-dispute-audit-contracts-verification.json)。其中的模拟协议只验证核验器，不计为真实模型证据。命令和状态含义见 [DISPUTES.md](runtime/DISPUTES.md#全部初始角色来自真实模型的前瞻案例)。

## 应用代码与执行环境冻结

`medic-runtime-p2-v3` 引入应用发布清单和每 run 的持久化执行身份，见 [BUILD.md](runtime/BUILD.md)。新建时冻结代码/资源清单 hash、Python 版本及可执行文件 hash、依赖版本；领取任务、MCP 工具授权、模型请求预留、恢复与发布均验证身份。工作线程协议、任务 context 和数据库报告携带同一身份，调用方不能覆盖报告的发布身份。取消、预算结算与只读历史查询仍可进行。

历史运行没有当时的完整应用清单，不能事后补写成已冻结。它们保持可查、可取消；新实现拒绝继续执行或自动绑定为当前版本。历史已完成验收的原始结论保留，新运行使用 `*_current.json` 合成夹具。

新发布的真实六角色运行 `run-feb0bac3ed4944218bed4463b94989eb` 已完成，[独立验收通过](evals/results/p2-build-live-verification.json)：六个角色均为真实模型、独立线程，创建事件、任务上下文、worker 协议和最终报告的应用身份一致。首评/二评均 B，Judge 与硬规则通过，状态为合成 `ready_for_human_review`；34 次请求、316,529 tokens，无未结算请求。没有修改冻结代码、增加预算或恢复旧运行来冒充此次验收。

完整回归 **146 项通过，其中 50 项在真实 PostgreSQL 执行**，耗时 297.831 秒，见 [构建身份与回归摘要](evals/results/p2-build-contracts-verification.json)。覆盖文件增删改、运行中的旧进程拒绝新清单、跨进程身份与预算保留、代码/依赖身份不匹配时禁止工具/请求/恢复/发布、历史运行不自动补写身份，以及报告身份不可由调用者伪造。首次测试因独立数据库启动端口错误出现 50 项连接超时；修正端口后完整重跑通过，失败日志保留且不计为通过。测试实例已停止。

## 尚未完成

- v7 已修复可复现的网关交接误拒绝和 SQLite 只读写锁竞争，完整回归与新的六角色真实闭环通过。v6 原服务异常细码缺失，原因仍不可追溯确认；持续故障/负载验证继续保留为待办。详见 [P3 修复及复验](P3_PROGRESS.md#v7-并发交接与只读轮询修复)。
- 全部初始角色已完成两例真实争议与正式回应；包含真实消息往返和第三轮的完整案例、更大范围的 Coordinator 决策容量与协议负载验证仍待完成。
- 长期并发/故障压测、生产隔离与持续调度服务部署；已完成的单次多材料闭环不替代持续负载验收。
- 客户部署镜像、只读发布目录、依赖制品锁定；已通过的应用身份绑定不是生产供应链签名或安全隔离验收。

P3 已实现解析与不可变材料库、三类角色界面、应用 API 和合成人工改分流程，详见 [P3_PROGRESS.md](P3_PROGRESS.md)。旧解析器只读审查和合成缺陷复现见 [迁移核查](ingestion/LEGACY_REVIEW.md)。P4/P5 所需专家集、签收门槛及客户部署/SSO 资料尚未准备好，不能据本地测试声称业务质量或上线验收通过。

本地持续调度服务已在 `medic-runtime-p3-v5` 实现，并通过 HTTP 新建后自动执行六个真实角色的合成探针；单实例互斥、有限恢复和 Windows 进程树验证见 [SERVICE.md](runtime/SERVICE.md)。此进展补齐本地自动入口，仍不替代上列长期负载与生产部署验收。

随后补齐一次真实 Windows 调度进程强制终止与 SQLite 续接：观察到的 16 个后代退出，原 Coordinator 结果保留，两个中断节点以新 epoch 完成，旧 binding 被拒绝，预算和截止时间未重置，最终只发布一次报告。详细证据与人为提前到期租约的边界见 [P3 故障恢复记录](P3_PROGRESS.md#真实调度进程强制终止与续接)。该单次故障不代表持续压测或 PostgreSQL 调度故障验收。


## 2026-10-23：v13 第三个前瞻真实案例

新增 `direction-ledger-attribution`，在启动前冻结 8 段虚构操作流程记录、输入 hash、当前执行身份和预算（120 次请求、1,200,000 tokens、并发 2、3,600 秒）。内容覆盖倒数指标与区间方向、互补事件口径、编辑作者/最后保存账号及提示后订正，未预填任何角色结果，也未修改 v5 角色指令。生成器修复新案例继承旧 runtime 版本的问题，支持选择登记范围及在启动前固定预算；12 项核验器/生成器契约测试通过，首次旧测试写死段落数的失败日志保留。

运行 `run-d8b0a309cd0e44c494ef9799bbb14a6e` 完成全部 6 个真实独立角色，各自首次 lease epoch 完成。首评/二评均 B，Challenger 实际提出 **0 个争议**，Judge 与硬门禁通过，进入合成 `ready_for_human_review`，仅发布一次预评估。**36 次请求、380,827 tokens、0 笔未结算**；无网关准入拒绝、任务失败、结构拒绝或原线程补正事件。请求开始至最后结算约 382.526 秒，属于本机这次观测，不作为生产吞吐承诺。

[16 项独立执行检查通过](evals/results/p2-dispute-direction-v13-verification.json)，输入、预算、发布身份、所有已完成 epoch、线程、工具引用与报告一致。实际 **0 条消息、0 次第三轮、0 个旧 blocking 问题承接**；覆盖状态明确保持 `partial`。此案不是第三轮通过证据，也不能通过与先前含 fixture 的探针拼接来补齐完整真实链路。v7 两例旧记录、v13 新登记和全部实际输出分别保存，没有更换输入后重跑以制造覆盖。

人工快速检查还发现盲审开放问题中混入了对测试目标措辞/扫描范围的询问；这只是单例观察，不是专家标签或错误率。后续用例应把测试管理信息与业务目标措辞进一步区分，保留本例原输入及输出作为对照，不在既有运行上改 prompt 或结果。本地试用库的历史评估、运行与预算逐行核对未变；API 和队列仍使用冻结 v13。

全真实消息往返与第三轮的覆盖缺口依旧存在。继续扩大合成用例前应提出可检验的覆盖假设，不能要求角色制造争议来通过检查；同时推进不依赖这些偶发行为的持续负载、故障及部署验证。P2 不标记完成。

## 2026-10-23：持续事务竞争与自然租约恢复

新增 [运行时负载探针](evals/runtime_load.py)，使用独立测试数据库、4 个竞争进程、1 个只读观察进程，每 2 秒创建一个合成任务，持续 300 秒后排空。任务只领取 Coordinator 租约，按 3 秒自然到期恢复，第三次尝试耗尽后取消；没有 SQL 提前到期、重置截止时间、提交模拟角色结果或发布报告。预留的 100 单位仅为模拟记账，**真实模型调用为 0**。

SQLite 场景完成 **150 项任务、450 次租约、300 次恢复**，23 项检查全部通过；记录到 1,342 次竞争后未领到租约和 2,927 次快照读取。约第 100 秒确认一个进程持有有效租约和未结算预留后强制终止，实际解释器句柄确认退出；替代进程继续处理，包含被终止进程在内的 450 个旧回调全部完成拒绝检查。唯一中断预留仍按 100 单位计入总预算，没有伪造结算。随后独立只读 SQL 再次核对全部任务、输入、账本、截止时间和构建身份，与原观测一致。见 [SQLite 证据摘要](evals/results/p2-transaction-load-sqlite-v13-verification.json)。

探针核验器的 **13 项测试通过**，包含丢失任务、重复 lease、提前恢复、预算丢失/虚假结算、截止时间重置、伪造报告、回调覆盖缺失、进程未真实退出及 libpq URI 解码的反例。没有应用代码变更，未重跑完整应用回归；见 [核验器测试摘要](evals/results/p2-transaction-load-audit-verification.json)。

真实 PostgreSQL 随后在独立 schema 中完成同样的 **300 秒、150 项任务、450 次租约、300 次恢复**；23 项检查全部通过，独立只读 SQL 对全部记录的复核也通过。同样执行一次真实进程终止与替代进程续接，450 个旧回调均已核验，保留且计入预算的未结算模拟预留为 1 笔。见 [PostgreSQL 证据摘要](evals/results/p2-transaction-load-postgres-v13-verification.json)。两类场景的限额、输入及应用构建均未更换；试用库六张关键表逐行 hash 未变，页面入口正常，负载进程已退出。

PostgreSQL 第一轮短探针在初始化前失败：测试工具将 URI 参数中的空格编码为 `+`，libpq 未将其解码为空格。该独立 schema 中创建了 0 张表、没有任务或模型调用；失败目录保留。修正为百分号编码并加入真实 libpq 解码测试后，新的短探针及上述正式场景通过。SQLite 300 秒证据对应修复前归档探针，最终工具另通过新的 12 秒 SQLite 场景；两份探针 hash 分别记录，不混称同一源码运行。应用发布清单始终为原 v13。

这是本地有限到达率下的数据库事务与进程故障验证，只轮询队列发现接口，不执行生产 `serve()`、完整任务图、网关或模型。因此不替代完整协议压力、长期稳定性、客户容量与生产服务验收，也没有补齐全真实争议消息和第三轮覆盖。运行方式及私有租约凭据保管边界见 [SERVICE.md](runtime/SERVICE.md#持续事务与进程故障探针)。
