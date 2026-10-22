# P1 单维度纵向闭环验收

日期：2026-10-17。结论：P1 合成单材料、单维度执行与边界验收通过。完整 PLAN 尚未完成；本记录不代表专家质量校准或客户上线验收。

## 真实执行证据

固定 P0 Codex 二进制，经本地 Responses namespace 适配层调用客户 DeepSeek。使用完全虚构的 M2 材料，含四段证据及一段恶意指令测试。未修改 Rust 内核，未启用 mock 或失败降级。

运行 ID：`run-9c806efbcbb04b9aa7110c06fdf3b375`。可公开的数量、版本及 hash 见 [p1-verification.json](evals/results/p1-verification.json)；完整私有快照与审计视图保存在 `medic/artifacts/p1/live-05/`，不提交仓库。

| 阶段 | 实测结果 |
| --- | --- |
| Coordinator | 真实模型通过 dispatch 工具创建全部五个子任务；未读取原文 |
| 首评 | 完成全部四个锚点检查，提出仅限本材料的 B 档建议 |
| 独立盲审 | 独立 thread；上下文 `prior={}`；未看到首评分数 |
| Challenger | 对首评提出一项归属方面的 advisory 反驳 |
| 二评 | 新 thread、revision 2；逐项回应并接受该反驳，维持 B 档及材料范围限制 |
| Goal Judge | 七项 checklist 完整，verdict 为 pass |
| Runtime | 当前硬规则重新验证通过；状态为 `ready_for_human_review`；`final_employee_decision=false` |

六个角色使用六个独立 Codex thread，所有证据角色均实际读取四段授权原文。每份结果须经 typed MCP 提交、服务端随机回执、成功 turn 终态校验后才记为完成。原始证据和各轮结果保持可追溯，报告包含引用跳转、反驳与回应、Judge checklist 和事件时间线。

本次成功运行耗时约 **448 秒**，32 个模型请求；服务端报告 249,864 input tokens、17,750 output tokens，总计 **267,614 tokens**，无缺失 usage 的估算请求。数字只覆盖此次成功运行，不含之前的诊断失败试跑。尚不能据此声称多 Agent 的质量或成本优于单 Agent；吞吐量与成本优化属于后续工作。

## 故障处理与回归

此前试跑均保留失败状态，没有发布为成功结果：

- tools 与最终 outputSchema 同时使用时，一次出现未调用工具却生成回执的行为。现由 typed submit 工具承担结构化输出边界，再核对真实回执；不据单次观察宣称端点普遍不支持组合能力。
- Windows 继承 GBK 编码导致中文 MCP 返回异常。stdio 显式采用 UTF-8，并通过强制 GBK 环境的子进程回归验证。
- 模型把真实回执包在 JSON 代码围栏中。适配器允许恰好一个完整 JSON 围栏；附加文本、多个对象和伪造回执仍拒绝。
- 引用连续原文片段曾被整段匹配规则误拒。现要求逐字连续片段加完整段落 hash；不自动修正或补造模型引用。
- 模型曾因上限为 C 而漏检 D 档锚点。提示词明确全量检查，校验器返回必需 ID；未完成检查无法提交。
- 初始请求时限不足以完成结构化业务输出。P1 实验参数调整为请求 150 秒、turn 240 秒，并保留工具数、token、并发和修复次数限制。

`python -m unittest discover -s medic/tests -q`：**54 项通过**。覆盖 P0 协议、假引用、组外评分、档位上限、未回应反驳、Judge pass 不覆盖硬规则失败、授权隔离、取消、旧租约晚到结果、有限恢复、重复发布、事务内重验门禁、编码和回执处理等。单元测试使用合成数据，不作为真实模型质量证据。

复验保存结果：

```powershell
python -m medic.evals.verify_run medic/artifacts/p1/live-05 medic/evals/results/p1-verification.json
```

## 后续阶段状态

- **P2 部分完成**：本地 SQLite 事务存储、租约与 outbox 登记、并发执行、有限重派、取消及审计。PostgreSQL、独立 outbox 消费、持久总预算、mailbox、多材料/维度、汇总复核与故障演练未完成。
- **P3 部分完成**：已将 P0 固定来源导入私有候选 Registry，17 项能力、49 组合、833 推荐值。模型与 policy 均未自动签收；解析器、三端应用与人工改分流程仍待完成。
- **P4 工程准备**：离线指标计算及其测试已加入。用户确认专家标注集和质量门槛签收尚未准备好，未进行质量验收。
- **P5 尚未验收**：用户确认客户部署及 SSO 接口资料尚未准备好；组织权限、运维、压测、备份回滚和客户试点仍待完成。

当前运行器仍拒绝非合成业务输入。生产 OS/容器隔离、留存与凭据生命周期尚未验收；P1 本地角色权限验证不能替代这些部署要求。
