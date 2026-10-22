# 回归与质量对照

2026-10-23 当前服务为 v16。回执恢复的真实受控故障探针通过，但完整 C 尚未通过；完整运行位于 `artifacts/p4/c16-full-01/`，已因引用校验失败而结束，预算和分析源码保持事先冻结。升级后的历史重放使用该目录的 `replay_v16.py`。只能在该运行终态后执行 `medic/.venv/Scripts/python.exe -m medic.artifacts.p4.c16-full-01.collect`，它同时核对完整任务图/依赖、初始及第三轮历史角色、门禁、数据库和用量回执，不把安全转人工等同于专家质量通过。多材料审计已按实际图包含 `aggregator`，新增初始材料/能力/角色范围覆盖检查。

v15 原失败记录不重新绑定到 v16。使用 `medic/.venv/Scripts/python.exe medic/artifacts/p4/c15-full-01/replay_v15.py` 在独立归档 v15 环境只读核验；旧 v14 共同输入实验继续使用下文 `replay_v14.py`。这些辅助脚本只核验各自既有本地制品，不启动新模型调用。

`fixtures/single_dimension.json` 是完全虚构的一份材料、一个 M2 维度，包含四段证据，其中一段是提示注入测试文本。它只用于执行链和边界验证，不是客户专家标注集。

该文件及旧版本夹具作为历史记录保留。当前应用发布新建运行使用 `single_dimension_current.json` 或 `multi_material_current.json`；见 [应用身份冻结](../runtime/BUILD.md)。不为旧 run 追溯补写 build 身份。`verify_run` 对新运行进一步核对创建事件、所有任务 context、worker 协议和报告的 build 身份；历史运行摘要会明确 `frozen_application_build_verified=false`，不据此伪称当时已有版本冻结。

`metrics.evaluate(labels, predictions)` 为后续单 Agent、线性 workflow 和多 Agent 对照提供同一统计口径。每行以 `case_id` 对齐；标签含 `grade`、`mode`、`cap`，预测含 `grade`、`evidence_valid`、`anchors_supported`、可选实测 `total_tokens`。缺失预测与合法弃权分别保留，缺失分值不填零；评分一致性和高估率仅在双方都有档位的样本上计算，并同时报告可评分覆盖率，防止仅保留容易样本。报告还包含加权 kappa、越权评分、证据不足评分和缺失用量数量。

指标函数不会生成专家标签，也不会自动签收质量门槛。当前测试只验证计算逻辑；`quality_acceptance` 固定标为未评定。用户已确认专家集、预签收质量门槛和客户上线资料尚未准备好，因此 P4/P5 仍待外部验收条件。

## 冻结 A/B/C 对照与离线报告

`medic.evals.comparison` 提供数据集与实验设计校验、盲执行计划导出、结果绑定检查和报告生成。它不执行模型，也不是 A/B/C 的执行适配器。A=`single_agent`、B=`linear_workflow`、C=`multi_agent` 的真实运行、原始协议核验和专家签收仍需接入。

以下两份 fixture 完全虚构，仅供命令试运行；其中模型、实现和 prompt 标识不是实际执行凭证。输出必须位于 `medic/artifacts/`、`medic/private/` 或 `medic/var/` 的私有目录，已有文件不会覆盖：

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.comparison freeze --dataset medic/evals/fixtures/comparison_dataset_synthetic.json --design medic/evals/fixtures/comparison_design_synthetic.json --output medic/artifacts/comparison-new/experiment.json
medic/.venv/Scripts/python.exe -m medic.evals.comparison schedule --experiment medic/artifacts/comparison-new/experiment.json --output medic/artifacts/comparison-new/schedule.json
# observations.json 由执行适配器或经过核对的离线导入产生；空数组也可生成明确的全部缺失报告。
medic/.venv/Scripts/python.exe -m medic.evals.comparison report --experiment medic/artifacts/comparison-new/experiment.json --observations medic/artifacts/comparison-new/observations.json --output medic/artifacts/comparison-new/report.json
```

### 输入与冻结边界

数据集每个 sample 表示一份评估输入，可含 1–17 个能力标签；预算、耗时和调用量按 sample 的一次运行计算，不能按能力重复累加。员工/事件标识应为数据集范围内稳定的脱敏标识，同一源文件的不同副本使用相同 SHA-256。开发集和测试集均须存在；任一员工、来源事件或文件 hash 跨集合即拒绝，包括跨员工共享事件形成的连通关系。工具只能检查所提供标识的一致性，不能发现被重新命名而未声明的同源关系。

标签记录能力、policy 分组、cap、可观察性及专家支持档位。非评分组保留空档位；评分组的不可观察标签也为空。专家档位不裁剪到 AI cap，否则会隐藏高档位的一致性缺口。真实专家数据要求每个 sample 声明至少两位不同独立评价者和裁决记录引用；这些字段不是对真实独立评价、授权或裁决的身份认证。合成 fixture 不需要伪造专家。

设计固定三方案的实现制品 hash、prompt hash、同一模型和 provider profile、重复次数（至少 2）、顺序种子及目标集合。`matched_tokens` 要求三种方案的 token 上限相同；`configured` 保留各自配置预算，两者分别冻结、不可混称等预算。相同上限不保证实际 tokens、金额或耗时相等，工具也不执行预算拦截。源材料、模型、矩阵、policy、parser 版本及分析工具的源码 hash、Python/Pydantic 版本一起冻结。改变分析工具后须使用旧归档环境复现旧实验，或明确创建新实验，不能沿用旧身份。

导出计划将每个 sample×方案×重复号恰好安排一次，用固定种子打乱顺序，只导出所选集合。计划包含输入标识、文件 hash、能力范围和 policy，不含专家档位、可观察性、评价者与裁决记录；它不会读取或传送材料正文。执行方应将这些引用解析为同一份真实输入，并自行保证会话独立和每个方案的执行语义。

### 观测导入与统计

观测是一组 JSON 对象，每条复制计划中的身份字段（不含 `input`），增加独立 `execution_id`、原始结果 `source_report_sha256`、`status`、`predictions`、`total_tokens`、`model_requests`、`retries`、`elapsed_seconds` 和 `human_review_seconds`。最后五项未测得时使用 `null`。每条预测为 `{code, grade, evidence_valid, anchors_supported}`，后两项未知可为 `null`；不能把未知证据核验当作已通过。`completed` 可以缺少部分能力，缺少项保留在覆盖分母；`failed` / `cancelled` 不得附最终预测，但其已消耗费用仍计入。重新尝试属于同一个计划槽的执行记录，不得重复导入同一槽来增加样本数。

导入拒绝重复槽位、重用 execution ID、计划外样本/重复号、越范围能力，以及模型、输入、prompt、实现或预算身份不一致。实际用量超过预算时保留记录并计入违规，不通过删除它改善结果。原始结果 hash 和 usage 属于导入声明；当前工具不会打开原始协议证明这些声明，不能据此认证真实模型运行或会话独立性。原始结果与协议应另行留存供适配器核验。

报告保留每方案、每重复、每岗位及每维度的一致性、加权 kappa、有符号/绝对档位误差、高估/低估、弃权、覆盖和违规；失败/缺失预测与合法弃权分别计数。整体覆盖以全部预定可评分标签为分母。C 对 A/B 的绝对误差比较只统计双方都有档位的配对单元，并同时给出未配对数量，不自动排名。重复运行的汇总单元相关，不能当作独立样本量，也不据此给置信区间或显著性结论。

费用按运行计一次，包含已导入失败/取消项。未知用量不计成 0，未导入运行也计入 unknown；全无测量时总量为 `null`。p50/p95 使用排序后 `(n-1)×p` 位置线性插值，仅针对有测量值的运行（包括失败），不是缺失运行的推断耗时。报告始终不自动签收质量门槛。

20 项定向测试与 12 项独立 CLI/数值核验已通过，见 [核验摘要](results/p4-comparison-verification.json)。后者使用预设错误、违规、失败、取消和缺失的合成观测，模型调用为零。实际三方案执行对照、专家标注、签收阈值、证据支持/归属抽检、轮次纠错/Judge 指标和正式统计设计仍待完成；见 [P4 进展](../P4_PROGRESS.md)。

## C 方案运行前绑定与收集

`medic.evals.comparison_runtime` 将 C 方案的计划槽与实际 runtime 连接，当前只允许合成技术实验。可执行 sample 必须提供 `assessment_content_sha256`：对 `AssessmentInput` 规范化后，保留全部业务字段（人员、周期、目标、材料原文与证据、标准、policy 等），版本字段只保留模型/矩阵/policy/parser 四个业务版本，再计算 canonical JSON SHA-256。实现、角色 prompt、Codex revision、provider profile 和实际模型分别核对。可通过 `content_identity(source)` 计算此值。未提供完整业务输入 hash 的旧离线 fixture 仍可用于统计测试，但不能启动 C 方案。

```powershell
# experiment.json 必须提前冻结完整业务输入 hash、实际 C 实现/prompt 和 token 上限。
medic/.venv/Scripts/python.exe -m medic.evals.comparison_runtime prepare --experiment PRIVATE_EXPERIMENT.json --input PRIVATE_SYNTHETIC_INPUT.json --sample SAMPLE_ID --repeat 1 --limits PRIVATE_LIMITS.json
# prepare 返回唯一 folder 和 run_id；运行时仅恢复此已创建任务，不再 --input 新建另一个。
medic/.venv/Scripts/python.exe -m medic.runtime --resume-run RETURNED_RUN_ID --binary codex-rs/target/debug/codex.exe --api-example '<本地 provider 配置文件>' --output RETURNED_FOLDER
medic/.venv/Scripts/python.exe -m medic.evals.comparison_runtime collect --folder RETURNED_FOLDER
```

每个实验槽在 `medic/artifacts/comparison-runtime/` 下使用单层完整 hash 的固定目录。完整绑定和输入先写入，再以实验/sample/方案/重复号派生 creation key 创建 run。重复准备或创建中断后的恢复复用原 run、截止时间和预算，取消/终态任务不会重置。无绑定的已有目录、修改过的绑定、旧双层长路径目录均拒绝；已有旧目录须使用当时归档适配器，不允许通过新路径获得第二份预算。这是单机单工作区约束，不是跨机器的分布式实验调度锁。

收集时检查确定性的 run ID、完整输入、预算、构建与 canonical 目录，再核对数据库/回执和发布归档。成功路径额外调用既有角色审计器，核对当前及历史完成轮次的 thread、turn、prompt、context、结果、引用、事件和安全人工出口；实际未出现争议/第三轮不被填造。失败/取消输出保留状态、费用和空预测，明确没有成功执行证明。`source_report_sha256` 指规范化的整个 `run.json` 快照 hash，因此失败时不需要虚构成功报告。成功路径的 `evidence_valid=true` 仅表示引用定位校验已过；`anchors_supported`、传输重试和人工耗时仍为未知，不能把引用存在当作行为判断已被专家支持。

输出 `comparison-export.json` 保留绑定、审计与测量；`comparison-observations.json` 可用于当前冻结实验的离线报告。收集不更改原运行、不执行额外模型请求、不自动定稿业务结果。A/B 执行器见下节；实际闭环状态以 [P4 进展](../P4_PROGRESS.md) 为准，真实专家标签、签收门槛和完整三方案重复对照尚未完成。

## A 单 Agent 运行与收集

`medic.evals.baseline_a` 使用一个独立 Codex thread/turn 处理全部输入范围，不拆分任务，也没有 Challenger 或二评。沿用同一已核验 provider、Codex 二进制和 namespace gateway；本地账本独立于 C 的任务图。只允许冻结的合成数据集与完整业务输入绑定，模型请求前保存实验槽、prompt、实现和限额；不会发布应用评估或人工定稿。

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.baseline_a --experiment medic/artifacts/EXPERIMENT/experiment.json --input medic/artifacts/EXPERIMENT/input.json --sample SAMPLE_ID --repeat 1 --binary codex-rs/target/debug/codex.exe --api-example '<本地 provider 配置文件>'
```

设计中的 A prompt hash 必须为 `digest(baseline_a.PROMPT)`，实现 hash 必须为 `baseline_a.implementation()`；执行顺序应从冻结的 `comparison.schedule()` 读取。仅启动 A 的技术测试需明确保留 B/C 未执行槽，不能当作三方案随机对照。

当前 A v2 单次输出上限为 16,384 tokens，每槽最多 20 次请求、并发 1、900 秒；总 tokens 上限来自冻结设计。输出 schema 按输入能力和标准数量生成，本地仍逐项验证全部 criterion_id、引用原文/hash、评分组、档位上限和低档标准覆盖。高于 AI cap 的标准及非评分组仍需逐项核查，不能省略；模型 schema 强制执行能力未获证明，最终以本地验证为准。一次全范围输出的上限与 C 单角色输出上限不同，等总预算不代表实际费用相等。

每槽固定在 `artifacts/comparison-a/<完整 hash>/`，SQLite 预算预留先于传输，估算与未结算用量不释放。进程锁防止同槽并发。终态重放只收集结果；进程中断后标记 interrupted，不重新获取预算或自动重跑。失败保留空预测与费用，无 mock、自动补分或修复模型输出。`attempt.json` 保留已观察到的协议事件，即使输出解析或核验失败也不丢失会话证据。

成功收集重新核对单会话的开始/完成事件、原始最后消息、结果、输入和 prompt 绑定，禁止工具执行；费用须逐条匹配网关回执与持久结算。`evidence_valid=true` 仅表示引用定位可核验，行为支持与人工耗时仍未知。A 结果包含 `outcome.json` 和可导入比较器的 `observations.json`；本地文件一致性不等于 provider 签名证明或专家质量验收。

B 的原六步结构、源码身份及迁移差异见 [B 基线迁移依据](BASELINE_B_MIGRATION.md)。原流程包含目标推荐值输入和 mock 降级，不能直接视为当前约束下的有效基线。

## B 六步迁移版运行与收集

`medic.evals.baseline_b` 实现固定六步数据流：本地原文锚点索引 → 三批证据提取 → 三探针反向核查 → 档位判定 → 独立会话 QC → 汇总。全 17 项输入需要七个模型会话（三批提取各一个，其余四步各一个），每个会话只执行一个 turn。第 6 步摘要也是计费模型调用。它没有 C 的单维度角色循环或返工图。

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.baseline_b --experiment medic/artifacts/EXPERIMENT/experiment.json --input medic/artifacts/EXPERIMENT/input.json --sample SAMPLE_ID --repeat 1 --binary codex-rs/target/debug/codex.exe --api-example '<本地 provider 配置文件>'
```

设计中的 B 标识由 `digest(baseline_b.PROMPT)` 和 `baseline_b.implementation()` 提供。实现身份包括 B、其复用的 A 账本/结果验证、业务输入绑定帮助模块与应用发布清单。完整输入和实验槽在调用前冻结；客户端不接收标签。B 仅允许合成数据，本地目录为 `artifacts/comparison-b/<完整 hash>/`。相同槽的终态重放只做收集，进程中断后不自动重新调用；独立进程锁、持久账本和未知费用口径沿用 A。

每槽最多 40 次请求、并发 3、900 秒；总 tokens 上限取冻结设计，单请求输出上限 16,384。v4 单请求网关上限为 300 秒，并写入运行绑定；前三版为 150 秒。三个提取批次共用同一账本，失败后等待已开始的批次退出并结算，不再启动依赖步骤。总运行截止时间限制新请求准入和后续 app-server 等待；不能靠进入下一步刷新截止时间。

提取只允许证据、来源类型、跨人/机制标记、覆盖比例、置信度和缺失项，不给档位。反向核查逐项保留三探针、证据强度、可核验的来源事件以及六种待核查信号。风险组保留三类红线扫描，不以“未命中”推导合规。所有引用均核验原文/hash，超过 200 字的引用拒绝。强度、归属、信号含义和行为支持仍需专家评价，程序只核对结构及引用。

评分覆盖全部能力和标准；非评分组、超过上限和缺少低档标准支持的档位拒绝，不事后截档。QC 不接收 step2 或会话历史，但读取完整冻结标准/原文锚点及 step3/step4；这是有先前判断的独立复核，不是盲评。QC 只能降低或撤销档位，`to_grade=null` 会清空最终档位并保留撤销理由。block 必须显式撤销所有原档位；需人工的 finding 和同项两个不同信号不能在有档位时被悄悄放行。六信号规则是本次 B 迁移试验的冻结规则，不代表客户已签收新的生产 policy。

每个模型步骤保存精确请求、schema、原始文本、结果和协议事件，核验后才交给下一步。收集再次按相同数据流重建各步请求，检查历史输出到后续输入的绑定、每一步开始/完成事件、最后消息、工具禁用和会话独立性；不能仅凭汇总 JSON 标记成功。最终输出包含各步骤、QC 修订前后判断、原文索引与摘要，`observations.json` 供离线比较。任何一步失败都保留空预测和全部已消耗费用，没有修复重试、mock 或通用摘要兜底。

与旧 B 的明确差异还包括：使用全部冻结原文锚点而非正则锚点/9000 字符截断，完整源输入超过 200,000 UTF-8 字节时执行前拒绝；没有复制旧静态客户模型、岗位推荐值或私有 prompt 正文。原标准与策略由绑定输入提供，当前结果仍不构成旧代码原样运行或三方案质量验收。

B v3 的提取和反向核查 schema 按输入顺序固定每个能力位置，拒绝重复编号或把标准编号当能力编号。预处理另生成 `citation_choices`：短段完整保留，长段按最多 200 字、相邻重叠 20 字生成逐字引文，覆盖全部原文；完整 spans 同时保留。模型只能复制一个合法引文对象，schema 将 evidence_id、span_hash 和可选 quote 联合绑定。服务端不替换或修复返回引用，任何不匹配仍失败。这个引用接口是迁移差异，应在后续三方案实验设计中明确冻结；引用可定位不等于它支持模型结论。

B v4 在反向核查 schema 和提示中明确：`strength=medium/strong` 的同一行必须有引用，`weak` 可为空。该规则原已由业务校验强制执行，此次前移到请求约束；旧失败输出仍失败，不补引用或降低验证标准。超时及约束调整后必须冻结新实验，不能重开旧终态槽。

B v5 以 `OUTPUT_SCHEMA_JSON` 在提示正文中重复同一个完整 schema，协议 `outputSchema` 仍保留；通过真实调用构建器的测试核对两者一致。该改动增加输入费用，不改变源材料、评分规则或本地拒绝条件。实际通过状态见 P4 记录。

## A/B/C 统一串行执行

`medic.evals.comparison_suite` 在任何模型调用之前校验所有计划槽的完整源输入、三方案实现/prompt 和 C 预算。`sources.json` 是 sample_id 到完整 AssessmentInput 的映射；`c-limits.json` 是同一组 sample_id 到 C 的 BudgetLimits 的映射。两者必须精确覆盖冻结设计所选 split，拒绝缺失或额外样本。当前仍只执行合成数据。

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.comparison_suite --experiment PRIVATE/experiment.json --sources PRIVATE/sources.json --c-limits PRIVATE/c-limits.json --binary codex-rs/target/debug/codex.exe --api-example '<本地 provider 配置文件>'
```

控制器按冻结随机顺序逐槽执行，先保存启动标识，再持久保存 observation 后推进下一槽。失败和取消观测继续纳入报告，不能替换成成功预测。运行目录为 `artifacts/comparison-suites/<experiment hash>`；控制器源码 hash、全部绑定及实验身份固定，进程锁阻止同一实验被重复调度。槽记录只能形成连续前缀，错槽、重复 execution_id 或与后端不符的 checkpoint 均拒绝。每个已完成前缀生成独立报告，最终保留全部 observation 与比较报告。

再次启动先用各方案适配器核验已有终态，不直接信任 checkpoint。A/B 已开始但未完成的槽沿用其账本中断语义，不重新调用模型。C 使用既有 runtime 子进程启动握手和 Windows Job；已存在的 C 目录只收集，不自动重新执行。若 C 缺少终态文件，控制器停止，须先检查该原 run 的真实进程和账本，再显式按 runtime 恢复流程处理；不删除目录、重置截止时间或分配新预算。控制器完成不等于每个模型运行成功，也不等于专家质量验收。

当前同总 token 上限方案仍有各臂请求数、并发、单请求输出和上下文方式的差异，报告须同时披露。预检、执行与结果收集证据见 [P4 进展](../P4_PROGRESS.md)。

统一入口现在按每个样本的必需任务图检查 C 的请求数下限：每个下游角色至少一次模型请求，再加真实 Coordinator 至少一次。低于下限时，任何 A/B/C 槽都不会启动，也不会创建 campaign 目录；CLI 返回 `comparison_suite_c_request_budget_infeasible`、`minimum_requests` 和 `configured_requests`。通过只表示未违反该必要下限，工具续轮、返工和实际 tokens/耗时可能需要更多预算；这不是完整可行性承诺。通用 runtime 与独立 `comparison_runtime prepare` 暂不包含这一新增检查。

控制器源码和预检内容变化会产生新绑定，不用新控制器重绑旧实验。升级至 v15 后，已结束的 `abc-all17-01` 使用已恢复的独立 v14 源码目录，执行 `medic/.venv/Scripts/python.exe medic/artifacts/p4/abc-all17-01/replay_v14.py` 只读复核：校验源码归档、使用原控制器并要求全部槽已终态，绝不启动或续接模型任务。旧预算不足、失败及提前取消如实保留；此本地历史辅助脚本不替代新实验的冻结与准入流程。不要直接用 v15 源码核验 v14 的执行绑定。

## 运行记录与真实用量核对

`medic.evals.runtime_measurements` 在一次只读数据库事务内核对已有 `run.json`、最终 `gateway-summary.json`、任务/结果、创建与发布事件、预算账本和执行身份。SQLite 用 `mode=ro`、`query_only`；PostgreSQL 用 `REPEATABLE READ READ ONLY`，不创建 Store、不执行 schema 迁移。对已结束运行才输出测量，源数据库及运行目录保持原样。对应发布清单与源码 ZIP 逐成员校验，可检查旧版本归档而不把它重绑到当前 v14。

```powershell
medic/.venv/Scripts/python.exe -m medic.evals.runtime_measurements --folder medic/artifacts/EXISTING-RUN --database medic/artifacts/EXISTING-RUN/runtime.sqlite3 --manifest medic/deploy/releases/RELEASE.manifest.json --archive medic/artifacts/releases/RELEASE/source.zip --output medic/artifacts/measurement-new/result.json
# PostgreSQL 使用 --database-env MEDIC_MEASUREMENT_DSN 替代 --database，连接内容不打印。
# 若有重启前的网关文件，可重复添加 --receipt-file <文件>；文件须含 requests 数组。
```

最终网关摘要中的持久账本必须与数据库完全一致。模型回执按 reservation ID 对齐，同一回执跨文件重复只计一次，内容冲突、跨运行回执、用量负值/非有限值、input+output 与 total 不等、回执和结算不一致均拒绝。`request_reservations` 是预留数，不保证每项已经发给模型；`receipt_verified_requests` 是完整用量回执核对数。实际总 `total_tokens` 只有每一笔都被回执证明时才有值；含估算、未结算或缺回执时为 `null`。另列已核对的实际用量小计与账本保守占用，不能把后者当成实际消耗。网关单进程的 `charged_tokens` 不用于代替跨重启总量。

结果中的时间是数据库创建到终态事件的间隔，包含排队与停机；没有终态时间时保留未知，不从文件修改时间推断。`technical_requeues` 是事件中的技术重排次数，不等同于传输重试次数。报告档位只与当前结果、冻结版本和发布 hash 对照，原 gate 原样保留；证据有效性、支持程度及人工耗时为未知。此工具不重新审查角色协议或执行硬 gate，也不证明本地回执是 provider 签名事实。

测量文件本身不是可直接导入 A/B/C 的 observation，明确标记 `not_admitted_requires_prospective_schedule_and_input_binding`。上节的 C 适配器补充运行前计划槽、完整输入、方案、模型、prompt 与预算绑定及协议核对后，另行生成 observation；普通历史测量不能绕过这层绑定。A/B 使用独立账本与协议收集，完整三方案对照仍待完成。

12 项测试通过（含 1 项真实 PostgreSQL），并只读核对旧 v13 真实合成运行的 36 条回执、380,827 tokens；重复网关文件未使计数翻倍，独立 SQL 与源文件/七张表核验一致。该数字属于历史运行，本轮新增调用为 0。见 [核验记录](results/p4-runtime-measurements-verification.json)。

`results/registry-import.json` 仅记录本地候选 Registry 导入的数量、hash 和待签收状态；模型正文与岗位推荐值实际保存在 `medic/private/registry/`。导入验证固定 P0 来源 hash，拒绝把旧 41 组合 / 697 条数据充作 260914 基线。

`python -m medic.evals.verify_run <private-run-folder> <summary.json>` 按当前完整任务图重新检查单/多材料运行、当前 attempt 完成事件、盲审隔离、硬规则和报告一致性，输出无原文的合成验收摘要。当前版本还要求每个 worker 留下实际 prompt bundle hash 和协议完成记录。旧批次没有记录 prompt hash 时该新增检查会失败；不能事后补写日志来冒充原始执行证据，历史验收摘要按其当时检查范围保留。

`postgres_faults.py` 的四阶段实际数据库故障探针见 [部署验证说明](../deploy/README.md)。配置测试 DSN 后，`test_postgres` 在真实数据库上复用业务契约；未配置时明确跳过，不将 SQLite 结果宣称为 PostgreSQL 通过。

`service_crash_probe.py` 验证 Windows 调度解释器在真实模型请求期间被强制终止后，同一合成 run 的续接。探针拥有独立 SQLite 和进程树，保留旧预算与已完成结果；先证明未到期租约不会抢跑，再显式注入提前到期以缩短等待。它不会宣称验证了默认 480 秒等待、生产高可用或模型评分准确率。启动方法、协议跨 attempt 汇集和未结算请求口径见 [持续服务说明](../runtime/SERVICE.md)；已通过记录见[进程故障恢复摘要](results/p3-service-crash-verification.json)。
