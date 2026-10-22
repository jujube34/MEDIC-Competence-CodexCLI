# 合成评估 runtime

当前入口只接受合成材料和真实 Codex/DeepSeek 执行。P1 单材料单维度已通过真实验收；P2 已实现跨材料汇总及其独立复核链、SQLite 与 PostgreSQL 存储。PostgreSQL 契约与实际中断恢复已通过，真实多材料执行结果见 [P2 进展](../P2_PROGRESS.md)。客户业务数据入口保持关闭，P2 全阶段仍未验收。

网页/API 新建任务可通过[持续队列服务](SERVICE.md)自动执行。该服务复用下述运行入口与持久账本，当前采用 Windows 进程树管理和每数据库单实例，不替代生产部署验收。

```powershell
python -m pip install -r medic/requirements.txt
python -m unittest discover -s medic/tests -q
python -m medic.runtime --input medic/evals/fixtures/single_dimension_current.json --binary codex-rs/target/debug/codex.exe --api-example <本地凭据示例路径> --output medic/artifacts/p2/new-run
```

当前新建使用 `single_dimension_current.json` 或 `multi_material_current.json`，分别是单材料单维度与两材料×两维度 31 节点夹具，均冻结 v5 指令和当前 runtime。多材料可配 `multi_limits.json` 实验预算，可加 `--plan-only` 只执行 Coordinator。其他同名旧夹具保留为历史证据；旧 `multi_material.json` 的 21 节点图不满足当前汇总链门禁。v2/v3/v4/v5 分别保存，输入版本不会自动升级。

已发布 prompt 的规范化 hash 记录在 `prompts/manifest.json`，内容修改须发布新版本。执行入口在新建/恢复前核对声明的 Codex revision、provider profile 及真实端点/model；worker 校验实际 Codex 二进制并记录所用 prompt hash。端点不匹配时要求重新完成 provider 准入，不能仅改版本标签继续运行。

应用文件及执行环境冻结见 [BUILD.md](BUILD.md)。没有历史 build 身份的旧 run 只读保留；新实现拒绝把它们继续执行为当前版本，也不接受改版本标签绕过恢复校验。旧回归记录仍可用 `medic.evals.verify_run` 只读核验。

凭据仅在本地读取。`artifacts/` 已排除版本管理；其中包含数据库、绑定凭据、角色会话和完整结果，应按私有运行数据管理。公开测试夹具全部为虚构内容。

Coordinator 经 MCP 为每个非 human_only 的材料/维度组合派发五个任务：首评和独立盲审可并行；随后是反驳、二评和目标审查。可批量派发，每个条目幂等返回结果或错误；完成计划前必须覆盖所有必需组合。human_only 直接形成 grade=null 的人工待办，禁止创建评分 Agent。每个角色有独立 Codex 进程、工作目录与服务端绑定的工具权限。盲审上下文没有首评。提交工具校验 schema、固定版本、引用、档位上限与反驳回应，生成随机回执；worker 还须观察成功的 turn 终态并核对回执，才推进依赖。失败不会转为 demo 或 mock。

引用允许完整原文或连续逐字片段，必须同时携带完整证据段落的 hash。材料中的指令作为数据处理；工具授权由租约确定，不接受模型自报角色。报告以数据库结果为准，`review.html` 仅为只读审计视图。

多材料任务每个维度追加 Aggregator、独立盲审、Challenger、二评和 Judge。Aggregator 等待该维度每份材料的二评与 Judge 完成；输出绑定来源结果 hash，按 `source_event_id` 去重。同一事件的重复材料不能声称多个独立佐证。汇总盲审仍没有任何已有评分；其余汇总角色可读取该维度全部授权材料，不能跨员工读取。最终报告的 `assessments` 仅列汇总二评，材料级结论另存为 `material_assessments`。

PostgreSQL 运行使用安装可选依赖的 Python，并用 `--database-env MEDIC_TEST_POSTGRES_DSN` 从环境读取 DSN；不指定则使用输出目录内的 SQLite。恢复和取消必须指定同一个数据库。部署及实际数据库故障探针见 [deploy/README.md](../deploy/README.md)。

最终回执可为纯 JSON、单个 JSON 代码围栏，或说明文字后的独立 JSON 尾段。多对象、嵌入式 JSON 与错误字段仍拒绝；格式兼容不替代真实提交记录、精确回执和成功 turn 终态校验。

默认预算：整个 run 模型并发 2、最多 80 个请求、500,000 token 保守总预算、30 分钟截止时间；单请求输出最多 4,096 token，请求 150 秒、turn 240 秒、租约 480 秒。每个 attempt 32 次工具调用，结果提交至多初次加两次修复。新 run 可用 `--limits <JSON文件>` 指定 requests/total_tokens/concurrency/wall_seconds；恢复时禁止修改预算。P0 探针仍使用自己的默认预算。

每次上游请求前，先在数据库原子预留请求数、token 和并发槽。不同网关实例共用 run 账本；恢复不清零。缺失 usage 或进程崩溃未结算时，按请求字节数与输出上限保守占用额度，不记成零；失联请求的并发占用到期后释放，但 token 预留保留。数据库不可用时不发送上游请求。取消/截止时间会使 worker 租约校验失败并中断回合。预算为实验参数，不是性能承诺。

Responses 流以 completed/failed/incomplete 终态为边界，不等待可选的 `[DONE]` 或连接 EOF 才结算和释放槽位。下一请求最多等待 2 秒让上一请求完成清理；真正超并发仍返回 429，并记录无内容的准入拒绝摘要。不会通过增加并发或忽略预算绕过限额。

租约恢复及取消入口：

```powershell
python -m medic.runtime --resume-run <run-id> --binary codex-rs/target/debug/codex.exe --api-example <本地凭据示例路径> --output medic/artifacts/p1/run
python -m medic.runtime --cancel-run <run-id> --output medic/artifacts/p1/run
```

恢复只重派失败或已过期的任务，每任务最多三个 attempt，保留旧结果和事件；完成节点不重跑。未验证终态的已提交结果不能直接当成功。旧租约在重派时失效，旧 worker 无法覆盖新结果。outbox 消费与租约领取在同一事务中；多个 dispatcher 可以共同排空队列，但不能抢占有效租约、超出 run 并发限制或提前启动未满足依赖的任务。Coordinator 未完成计划时，其子任务不启动。

v5 新增受限 mailbox、Coordinator 决策的第三轮定向返工、下游失效及旧争议的显式回应。两次往返上限、历史归档、人工出口和独立技术重试计数见 [DISPUTES.md](DISPUTES.md)。成功回合缺少真实提交/回执时，可在原线程内进行有界完成补正，仍受原租约、工具和总预算约束。

本地持续调度服务已实现，入口与验证范围见 [SERVICE.md](SERVICE.md)。尚待 P2 完成：初始阶段也来自真实模型的争议全链案例、长期并发/故障压测、生产调度部署，以及客户部署制品与环境验收。真实执行的逐项证据与未完成项以 [P2_PROGRESS.md](../P2_PROGRESS.md) 为准；局部通过不替代 P2 全部验收。

P4 专家标注集、质量门槛签收记录，以及 P5 客户部署和 SSO 接口资料，用户于 2026-10-17 确认尚未准备好。工程开发可继续；业务质量与客户环境验收不能据此宣称通过。
