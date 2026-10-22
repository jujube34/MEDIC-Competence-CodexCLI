# 有界争议与第三轮返工

v5 指令启用以下协议；v2–v4 保留原工具边界。业务角色仍由 Codex/DeepSeek 执行，runtime 只校验、派发、留痕和控制预算。合成测试通过不代表专家质量或生产部署验收。

## Mailbox

`runtime_post_message` 只授予 Challenger 与 Re-evaluator，接收者必须是同 run、材料、维度内的对方任务。消息绑定已持久化的 challenge ID、claim ID、challenge 结果 hash、发送方当前租约和已读取的引用。Judge 可以读取会谈，不能发消息；盲审与 Coordinator 不读取会谈原文。

每个 topic 最多两个问题/答复往返，要求按顺序回复上一条消息；每个范围最多 16 条消息、每 run 最多 128 条。`message_id` 幂等，重复同内容不追加，改写同 ID 会被拒绝。新 attempt 可以引用相同的合法结果 hash，旧租约不能继续发送。消息都是待核验主张，不能充作新证据，也不能改变正式结果或关闭 challenge。

Judge 的候选 hash 包含完整范围内的会谈。审查后改写或遗漏消息会使候选不匹配；未答问题即使遇到 Judge `pass` 也无法自动放行。

## 定向返工

首轮任务图全部完成后，若 Judge 请求 `rework` 或仍有待答问题，runtime 最多一次重新排队 Coordinator。它在独立真实回合中读取固定候选清单，通过 `runtime_resolve_rework` 对每个候选明确选择第三轮或人工处理。决定不可覆盖；全部候选有决定、Coordinator 成功提交并结束后，子任务才启动。

第三轮只重新排队该范围的 Challenger、Re-evaluator、Judge，以及依赖它们的下游节点。材料级结果变更会使同维度 Aggregator、汇总反驳、汇总二评和汇总 Judge 失效。原始材料不变时，首评和独立盲审保留；其他维度保持原结果。每次失效先归档旧上下文、读证据记录、结果 hash、线程及轮次；旧结果仍以 lease_epoch 保存在结果表，旧租约立即失效。

第三轮 Re-evaluator 使用 `revision=3`。除当前 challenge_responses，还必须提交带原 challenge 结果 hash 的 `prior_dispute_responses`，逐项回应上一轮未解决的 blocking challenge。即使新的 Challenger 没再列出旧问题，程序也禁止省略。仍未解决的旧问题或未答消息进入人工处理；不会创建第四轮。

业务轮次与技术 attempt 分开：每个业务轮最多三个技术 attempt，lease_epoch 始终递增；总请求、token 和截止时间沿用同一个 run。Coordinator 选择人工处理时保留该决定，后续其他节点通过也不能抹去人工要求。

## 提交补正

模型回合成功结束但只说“准备提交”、没有合法回执时，worker 可在同线程内发起最多两次完成补正。补正与结果结构拒绝共享限制，原租约不延长，工具次数和总预算不清零。新 turn 必须先获得 runtime 补正额度，再绑定执行身份；旧 turn 无法完成新 turn 的结果。HTTP/执行失败不被当作成功回合补正。协议记录标识为 `bounded-finalization-v1`。

v16 增加 `assessment_get_submission_receipt`，参数为空，仅在当前绑定任务/租约内读取已持久化的回执和结果 hash；未提交则返回 `not_submitted`，不生成替代回执。补正提示要求模型先查询原回执，避免重复抄错自己的上一条回复。查询仍消耗原 32 次工具预算，取消、失效租约或旧 epoch 无法读取。它不会提交结果、改写评分或自动完成任务；最终仍须模型返回正确回执，由 `Store.complete` 精确匹配并核验执行身份。两次补正上限不变，继续抄错仍失败。真实受控故障验证与边界见 [v16 记录](../evals/results/p4-receipt-recovery-verification.json)。

## 验证边界

契约测试覆盖 SQLite/PostgreSQL、消息权限与重放、旧问题不能丢弃、单材料返工只失效本维度下游、显式人工出口、无第四轮、技术重试与业务轮数分离。

`python -m medic.evals.rework_probe --output medic/artifacts/<new-probe>` 创建明确标记为 fixture 的初始争议状态。随后用常规 runtime `--resume-run` 运行真实 Coordinator、Challenger、第三轮 Re-evaluator 和 Judge；`python -m medic.evals.verify_rework <folder> <summary>` 单独核验这些真实续接行为。初始结果不是模型产出，保留的首评/盲审始终标记 fixture，因此整份报告必须被完整 live gate 阻断，不能把该探针冒充全链真实评分验收。

真实续接探针已通过，见 [验证摘要](../evals/results/p2-rework-live-verification.json)。更多材料/维度的负载与 Coordinator 决策容量仍待验证；本地持续调度服务已实现，见 [SERVICE.md](SERVICE.md)。应用代码身份绑定见 [BUILD.md](BUILD.md)，不替代客户部署验收。

### 全部初始角色来自真实模型的前瞻案例

`python -m medic.evals.dispute_cases --output medic/artifacts/<new-suite>` 会在运行前保存选定的完全合成输入、输入文档 hash、固定预算和当前执行身份（默认登记全部案例）：一份涉及效应指标、修订和个人归属，另一份涉及总体/分层比例、样本选择及重复佐证。生成器不创建任何角色结果，也不修改已发布的 prompt。登记中的 `input_hash` 指解析后的输入文档；运行库另按补齐 schema 默认值后的输入计算 hash，核验器分别检查两者。

使用正常 `medic.runtime --input <case.json> --limits <suite>/limits.json --output <case-run> ...` 执行。保留所有实际尝试，不为获取争议修改已启动运行的材料、结果或预算。模型没有提出争议时记录覆盖缺口，不要求它制造分歧。

`python -m medic.evals.verify_dispute_live <case-run> <suite>/prospective-cases.json <case-id> <summary>` 同时检查当前和归档的每个已完成 epoch：初始六角色、结果和上下文 hash、提交/执行/完成事件、worker 协议、原 prompt 和构建身份、已读引用、消息的 challenge hash/发送者/顺序及发送时段。初始 worker 协议缺失、初始结果为 fixture 或旧结果被改写都不能由第三轮成功掩盖。

核验区分 `execution_status` 与实际 `coverage`。允许正确进入人工处理，不能把放行当作唯一成功结果；但结构完整性错误不能当作正常人工出口。整体 `pass` 还要求观察到真实争议、正式回应、消息问答、Coordinator 第三轮决定、四个第三轮角色及旧 blocking 问题的显式承接。执行正确而覆盖不全时返回 `partial`（退出码 3）。核验器的 11 项合成契约测试见 [测试记录](../evals/results/p2-dispute-audit-contracts-verification.json)，这些测试不是模型执行证据。具体实跑结果见 [P2 进展](../P2_PROGRESS.md)。


### v13 补充前瞻案例

新增 `direction-ledger-attribution` 合成输入，包含指标倒数/区间方向、互补事件口径、编辑作者与最后保存账号、提示后订正和同事件边界。所有数字属于虚构操作流程，不是医疗效果数据。预登记只提出覆盖假设，不规定模型必须生成争议、问答或第三轮。

生成器现按当前冻结发布版本填入新案例的 runtime 身份，避免继承历史 `single_dimension_current.json` 的旧版本字符串。`--case` 可重复指定登记范围；`--limits` 只在创建全新登记目录前读取预算 JSON，预算通过 `BudgetLimits` 校验并与输入、执行身份一起固定。不能用它修改已运行的预算。

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.dispute_cases --output medic/artifacts/new-dispute-suite --case direction-ledger-attribution --limits <new-suite-budget.json>
```

默认仍为每 run 100 次请求、750,000 tokens、并发 2、2,400 秒。v13 的补充案例单独前瞻登记为 120 次请求、1,200,000 tokens、并发 2、3,600 秒，为可能出现的第三轮留出空间；真实使用量和覆盖结论以独立核验结果为准。初始结果、prompt 或业务判定没有注入替代值。生成器/核验器另留源码 hash 与副本，12 项契约测试记录见 [v13 核验器检查](../evals/results/p2-dispute-audit-v13-verification.json)。
