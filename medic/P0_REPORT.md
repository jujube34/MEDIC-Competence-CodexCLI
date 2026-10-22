# P0 基线与可行性验收记录

日期：2026-10-17（Asia/Shanghai）。结论：**P0 技术可行性通过，准入 P1 合成材料闭环**。这不是业务版本签收、生产部署或员工正式评价授权。

选定接入路线：**固定 Codex app-server → 本地 Responses namespace 适配层 → 客户 DeepSeek Responses API**。不改 Codex Rust 内核，不调用外部模型，不降级成 mock。真实工具、结构化输出、进程重启恢复、4 thread 并发和中断均已执行。

## P0-01：数据基线和版本差异

对用户指定的本地资料目录只读检查，逐项读取能力定义、A–D 概念层和执行层，验证明细唯一键、档位与分值一致性、每个岗位的 17 项覆盖。来源文件 SHA-256、检查结果及字段级差异见 [assets.manifest.json](p0/results/assets.manifest.json)。未复制原始标准、岗位名称或推荐值到公开代码。

| 项目 | 实际核验结果 |
| --- | --- |
| 根目录旧模型 | 16 项，缺 E4；E 维度定义与交付版不同，E3 定义及八档锚点有变化 |
| 交付0908 模型 | MEDIC 五维、17 项；定义及 136 个概念/执行锚点均非空 |
| 260914 矩阵 | 49 个岗位职级组合 × 17 项 = 833 条；无重复键、缺项或档位/分值不一致 |
| 260908 demo | 41 个组合、697 条；相对交付模型，E3/E4 的执行层四档均有差异 |
| 新旧矩阵差异 | 新增 204 个岗位×能力键、移除 68 个；共同键中 50 条推荐值变化。净增 136 条，不只是追加 8 个岗位 |
| 260922 AI policy | 四组完整覆盖 17 项，分组为 8/4/2/3；E3 的细项表允许初评，分组总结要求仅提线索 |

初始 policy 候选保存在 [policy.candidate.json](p0/policy.candidate.json)：E3 暂取 `clue_only`，建议上限、D1 条件上限及禁止自动 D 均显式记录。`status=awaiting_business_signoff`，不把候选写成已签收版本。这里只证明结构完整与差异可追踪，不替代业务负责人确认锚点语义和唯一发布版本。

## P0-02：固定构建和 app-server 契约

- 源码 revision：`221e4e7c05386d06aa7b91da2a9b04f7f2bd921f`；实际工作区相对该 revision 的上游源码无改动。
- Rust 1.95.0；`cargo build --locked -p codex-cli --bin codex` 成功。生成的是 Windows MSVC debug 可行性二进制，版本字符串为 `codex-cli 0.0.0`，因此必须按 SHA-256 锁定，不能仅按版本字符串部署。
- 对固定二进制执行 `app-server generate-json-schema`，11 个 initialize/thread start/resume/turn start/interrupt/completed 核心契约与固定源码的生成 JSON Schema 一致。
- [baseline.lock.json](p0/results/baseline.lock.json) 保存二进制、Cargo.lock、Rust toolchain 和源码自带 318 个 JSON Schema 的 manifest 哈希。完整生成 schema 在本地忽略目录 `medic/artifacts/p0/generated-schema/`。

app-server 使用独立 CODEX_HOME 和空工作目录，稳定接口 `experimentalApi=false`。业务工具通过 MCP stdio 注册，不依赖动态工具实验接口。启动时先等待 MCP ready；中断测试等待流式输出后发出，不将 `turn/start` ACK 误认作活动 turn 已就绪。

## P0-03：真实接口能力矩阵

模型返回标识为 `deepseek-v4-flash`。只有合成输入被发送到客户接口。脱敏结果摘要见 [verification.json](p0/results/verification.json)，部署候选能力设置见 [provider-profile.json](p0/results/provider-profile.json)。

| 能力 | 结果与证据边界 |
| --- | --- |
| Chat Completions / Responses 连接 | 两者真实成功；均有 token usage |
| 扁平工具闭环 | 两种协议均成功；本地工具随机生成 receipt，模型必须准确返回它 |
| Responses 两次连续工具调用 | 成功；核对不同 call ID 和两次实际执行 |
| 工具报错后继续 | 注入合成工具错误，模型重新调用后成功 |
| 命名 tool_choice | 真实调用指定工具成功 |
| SSE | Chat `[DONE]` 与 stop；Responses created/item/delta/completed 均已观察 |
| JSON Schema | 两种协议的样本均通过本地 schema 和随机 marker 校验；不宣称服务端原生强制约束 |
| Codex namespace 工具 | **原生失败**：HTTP 成功但未产生 function_call。经适配层后 MCP 实际执行成功 |
| app-server 结构化结果 | 使用 outputSchema，结果通过本地校验 |
| 会话恢复 | 原进程关闭，新进程 thread/resume 后正确回忆随机 marker |
| 4 thread 并发 | 同时活动 turn 峰值 4；各自随机 marker 全部匹配 |
| 中断 | 流式输出中 turn/interrupt，收到 interrupted；网关观察到断连并关闭上游连接 |
| 上下文 | 18,085 字符合成输入检索 marker 成功；最大窗口和服务端配额仍未知 |
| 视觉、压缩、并行工具调用 | 未验证，P1 默认不使用；并发 thread 通过不等于并行工具调用通过 |
| 生产租户隔离 | 未验收；当前仅为配置收窄与合成环境测试 |

27 项本地测试覆盖未知工具、非法参数、跨证据 ID 拒绝、循环/请求预算、401/403/429/5xx 不重试、断流与截断失败、命名空间双向转换、call ID 与消息角色保留、客户端断开关闭上游、资产差异及验收门禁。它们是本地故障测试，与真实模型通过项分开记录。

网关抓取的模型工具清单中没有 shell、文件修改、view_image、web search 或 spawn_agent。仍有 MCP 资源工具与 request_user_input；服务端对非预期请求拒绝处理。故此不能声称仅靠配置已实现严格的生产能力白名单，接真实员工材料前仍需容器/OS ACL 与网络出口验证。

## P0-04：路线选择与最小复现

`provider.py::Provider.namespace_tool` 是最小合成复现：把同一个 `echo_evidence_id` 放入 `type=namespace` 的工具定义；客户端点返回普通文字而非实际 function_call。扁平工具往返成功，MCP 服务单独握手和 tools/list 也成功，说明不能用“HTTP 200”或“支持 tools”替代固定 Codex 的完整协议验证。

已核对 [LiteLLM Responses 文档](https://docs.litellm.ai/docs/response_api) 及其仓库的 [namespace 转换问题记录](https://github.com/BerriAI/litellm/issues/35878)。文档支持 Responses 并不证明某个固定版本能为此客户接口正确处理 namespace；本轮没有部署或认证某个 LiteLLM 版本，也不据问题记录断言所有版本不可用。现有客户端点已经支持 Responses/SSE，故只实现已复现缺口对应的最小适配层，没有另造 Chat Completions 转换服务。

适配层仅将 namespace function 映射为稳定、唯一的扁平别名，回传时恢复 namespace/name，保持 call ID、工具结果类型、消息顺序和流式终态。拒绝 custom/tool_search/deferred/stateful previous_response_id 等未支持路径。仅监听随机 loopback 端口，临时令牌鉴权，固定上游与模型；没有通用转发或外部兜底。

中断关闭的是上游 HTTP 连接，不证明客户服务器已停止推理或计费；取消请求缺失 usage 时保守估算，不能记为零。该适配器是可行性组件，正式内网服务的 TLS、部署运维、重试及 ACL 在后续阶段完成。

官方 [Codex App Server 文档](https://learn.chatgpt.com/docs/app-server) 用于核对接口类别；实际字段与测试以本仓库固定源码及生成 schema 为准。

## 验收与下一步

`python -m medic.p0 gate --directory medic/artifacts/p0` 的实际结果：`status=pass`、`p0_accepted=true`、`route=responses_namespace_gateway`。门禁同时核对固定二进制身份、源代码/生成 schema、资产审计、真实 provider 及同一上游端点/模型的 app-server 证据；缺一项不准入。

完整复跑命令见 [p0/README.md](p0/README.md)。客户地址、凭据、完整本地协议记录、会话和生成文件留在忽略目录；公开记录仅保留统计、哈希和合成检查结论。

下一步是 P1 的单材料单维度合成闭环。正式业务仍须完成模型/policy 和质量门槛签收、外部权限隔离、质量校准及生产部署验收。P0 没有实现或宣称完成这些后续工作。
