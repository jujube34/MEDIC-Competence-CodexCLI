# MEDIC Competence Agent Platform

以开源 Codex CLI 为 Agent 执行内核、以客户内网 DeepSeek 为模型，在外围实现员工胜任力评估的多 Agent runtime。

当前阶段：P0「基线与可行性」与 P1 合成材料真实模型闭环均已通过（2026-10-17）。P2–P5 尚未完成，完整生产评估平台仍在开发。

后续开发统一在 `jujube34/MEDIC-Competence-CodexCLI` 仓库进行，采用 2026-10-17 同步的最新版 Codex CLI 源码。固定开发基线与上游同步方式见 [UPSTREAM.md](UPSTREAM.md)。

- [开发计划](PLAN.md)：角色分工、单材料单维度评估、独立反驳、第二轮评价、目标审查、状态机、数据契约及分阶段验收。
- [上游接口与固定版本](UPSTREAM.md)：源码核对结果、Responses 协议约束、可复用能力和未验证项。
- [P0 验收记录](P0_REPORT.md)：真实 DeepSeek 联调、固定构建、模型/policy 差异及能力边界。
- [P1 验收记录](P1_REPORT.md)：六个真实角色闭环、引用与反驳、独立 Judge 和硬规则、耗时与用量、后续未完成项。
- [P2 进展与恢复证据](P2_PROGRESS.md)：持久预算、共享 outbox、跨材料汇总、数据库恢复、争议返工及应用版本冻结。
- [P3 材料与应用进展](P3_PROGRESS.md)：四种文件解析、私有证据快照、应用权限、三类角色工作台与人工复核；生产 SSO 待完成。
- [网页工作台](apps/web/README.md)：员工上传、上级复核、总监范围概览、原文定位和缺分呈现。
- [应用 API](apps/api/README.md)：本地合成环境启动、员工范围授权、上传、评估、人工修订与定稿接口。
- [持续队列服务](runtime/SERVICE.md)：自动发现新评估、单实例互斥、Windows 进程树管理和有界重启恢复。
- [P0 复跑说明](p0/README.md)：只读资产审计、provider/app-server 探针、本地测试与验收门禁。
- [合成 runtime 运行说明](runtime/README.md)：真实角色任务、授权工具、硬规则、审计视图与恢复入口；明确 P2/P4/P5 未完成项。

工作流：Coordinator 派发 → 单维度 Evaluator → Challenger → Re-evaluator → 同维度跨材料汇总 → Goal Judge → runtime 硬规则 → 上级复核。

Coordinator 不直接评估原始材料；Goal Judge 的通过意见必须同时满足程序规则；材料不足与未解争议可转人工。AI 预评估包通过并不等于员工最终评价已定稿。

新增代码均放在 `medic/`。上游 Codex 内核和根目录说明保持原状。客户原始标准、推荐值表、员工材料、凭据、运行数据与真实 trace 不提交到公开仓库。

P0 选定 Codex → Responses namespace 适配层 → 客户 DeepSeek 路线，已完成真实 MCP 工具调用、结构化输出、进程重启恢复、4 thread 并发及中断。后续按 [PLAN.md](PLAN.md) 逐阶段验收；正式业务仍需模型/policy 签收和权限隔离验收。
