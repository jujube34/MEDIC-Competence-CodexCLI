# MEDIC Competence Agent Platform

以开源 Codex CLI 为 Agent 执行内核、以客户内网 DeepSeek 为模型，在外围实现员工胜任力评估的多 Agent runtime。

当前阶段：架构与开发计划。尚未实现或验证真实评估平台。

- [开发计划](PLAN.md)：角色分工、单材料单维度评估、独立反驳、第二轮评价、目标审查、状态机、数据契约及分阶段验收。
- [上游接口与固定版本](UPSTREAM.md)：源码核对结果、Responses 协议约束、可复用能力和未验证项。

工作流：Coordinator 派发 → 单维度 Evaluator → Challenger → Re-evaluator → 同维度跨材料汇总 → Goal Judge → runtime 硬规则 → 上级复核。

Coordinator 不直接评估原始材料；Goal Judge 的通过意见必须同时满足程序规则；材料不足与未解争议可转人工。AI 预评估包通过并不等于员工最终评价已定稿。

所有新增业务代码计划放在 `medic/`。上游 Codex 内核和根目录说明保持原状。客户原始标准、推荐值表、员工材料、凭据、运行数据与真实 trace 不提交到公开 fork。

下一阶段从 [PLAN.md](PLAN.md) 的 P0 开始：先证明客户 DeepSeek 能经 Codex 完成真实工具调用闭环，再扩大多 Agent 开发范围。
