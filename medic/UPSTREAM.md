# Codex 复用依据与版本记录

检查日期：2026-10-17。固定源码已构建并完成 P0 客户 DeepSeek 联调，验收和限制见 [P0_REPORT.md](P0_REPORT.md)。本文保留固定基线的源码依据。

## 仓库与基线

| 项目              | 值                                                                            |
| ----------------- | ----------------------------------------------------------------------------- |
| 上游              | [openai/codex](https://github.com/openai/codex)                               |
| 当前开发仓库      | [jujube34/MEDIC-Competence-CodexCLI](https://github.com/jujube34/MEDIC-Competence-CodexCLI) |
| 当前主分支        | `main`                                                                        |
| Codex 开发基线    | `221e4e7c05386d06aa7b91da2a9b04f7f2bd921f`，2026-10-17 同步的上游最新 `main` |
| 新增内容范围      | `medic/`；业务开发在当前仓库进行，保持上游内核与业务代码分离                   |
| 许可              | 保留仓库 LICENSE/NOTICE；Rust workspace 标注 Apache-2.0                       |

已基于最新版 Codex CLI 建立独立开发仓库，并保留完整 Git 历史。本地目录为 `C:\MEDIC-Competence-CodexCLI`；`origin` 指向 `jujube34/MEDIC-Competence-CodexCLI`，`upstream` 指向 `openai/codex`。后续开发统一在当前仓库、以上述最新源码基线开展。

开发基线已确定。P0 已在该基线上完成 provider 与 app-server 兼容测试，实际构建的 revision、二进制哈希及生成 schema 记录在 [baseline.lock.json](p0/results/baseline.lock.json)。当前为 debug 可行性构建，不能作为生产发行包。后续同步上游时显式更新基线记录；禁止以浮动 `main` 直接部署。

## 已从固定基线源码确认的能力

以下相对路径均位于仓库根目录。链接固定到本次基线 SHA，避免以后行号漂移。

| 能力                      | 源码依据                                                                                                                                                                                                                                                       | 对方案的含义                                                                                         |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 自定义模型 provider       | [model-provider-info/src/lib.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/model-provider-info/src/lib.rs#L139) 定义 base_url、env_key、requires_openai_auth、supports_websockets                         | 可连接内网自定义服务，无需修改默认 OpenAI provider 或依赖个人 ChatGPT 登录                           |
| 只支持 Responses wire API | [同文件 WireApi](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/model-provider-info/src/lib.rs#L107) 只有 Responses，解析 chat 时明确报错                                                                      | 客户 Chat Completions 示例不直接兼容，必须验证 Responses 端点或适配网关                              |
| app-server stdio          | [cli/src/main.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/cli/src/main.rs#L572) 定义 app-server、listen 和 stdio                                                                                       | 外围服务可通过子进程驱动，不需要嵌入 Rust 内核                                                       |
| thread / turn 生命周期    | [protocol/common.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/app-server-protocol/src/protocol/common.rs#L500) 包含 initialize、thread/start、thread/resume、turn/start、turn/interrupt、turn/completed | runtime 可管理独立角色会话、事件和取消；业务任务状态仍自行持久化                                     |
| 角色指令与配置            | [ThreadStartParams.ts](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/app-server-protocol/schema/typescript/v2/ThreadStartParams.ts) 包含 modelProvider、cwd、baseInstructions、developerInstructions、config | 可注入窄职责上下文；这些字段本身不是业务授权系统                                                     |
| 每回合结构化结果          | [TurnStartParams.ts](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/app-server-protocol/schema/typescript/v2/TurnStartParams.ts) 定义 outputSchema                                                            | 可约束角色输出形状；引用真实性、评分上限和权限仍由 runtime 校验                                      |
| 动态工具是实验接口        | [protocol/v2/thread.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L145) 对 dynamic_tools 标注 experimental                                                 | 第一版优先 MCP；不能因为普通生成 TS 中没有该字段就断言源码不支持，也不能未经实验协议 opt-in 直接使用 |
| 原生 Agent 协作           | [multi_agents.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/core/src/tools/handlers/multi_agents.rs) 组织 spawn/send_input/wait/close 等 handlers                                                        | 有可进一步复用的基础，但不能替代业务权限、持久任务图、租约与审计；DeepSeek 实际调用仍待测            |
| 原生 goal API             | [protocol/common.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/app-server-protocol/src/protocol/common.rs#L615) 包含 thread/goal/set/get/clear                                                           | 能辅助跟踪会话目标；MEDIC 的评估放行必须另有独立 Judge 与程序 gate                                   |
| exec JSONL 与 schema      | [exec/src/cli.rs](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/codex-rs/exec/src/cli.rs#L54) 定义 output-schema 和 json                                                                                              | 用于小规模接入试验与回归，不把一串 exec 脚本当作完整生产 runtime                                     |
| TypeScript SDK            | [sdk/typescript/README.md](https://github.com/jujube34/MEDIC-Competence-CodexCLI/blob/221e4e7c05386d06aa7b91da2a9b04f7f2bd921f/sdk/typescript/README.md) 描述 CLI 子进程、JSONL、流式、恢复与结构输出                                                                       | 若后续采用 TS 可选用；Python 服务直接接 app-server 无须复制 SDK 内部实现                             |

当前开发基线的 `codex-rs/model-provider-info/src/lib.rs` 已确认只支持 Responses 并拒绝 `wire_api = "chat"`；客户接口兼容性仍需按此约束验证。

## 配置草案（未实测，不可直接用于生产）

以下仅展示固定基线存在的配置字段。示例 URL 是本地待实现的协议网关，环境变量名不包含真实凭据。客户真实地址与长期 API key 由内网部署配置管理。

```toml
model = "deepseek-v4-flash"
model_provider = "medic_deepseek"

[model_providers.medic_deepseek]
name = "MEDIC internal DeepSeek gateway"
base_url = "http://127.0.0.1:8788/v1"
env_key = "MEDIC_GATEWAY_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0

[analytics]
enabled = false
```

此草案将传输重试集中给外围策略，避免重复重试相乘；生产前应在固定版本验证零重试设置、SSE 中断及恢复语义。跨机器网关要使用客户认可的 TLS/认证，不能照搬 loopback HTTP。角色工具、沙箱、会话存储、网络和文件权限配置需在 P0/P1 单独验证，不因这一段 provider 配置存在就视为完成。

## 官方接口文档

官方参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server) 和 [Advanced Configuration](https://learn.chatgpt.com/docs/config-file/config-advanced)。官方文档用于理解生命周期、配置与接口类别；实现时以当前固定源码的生成 schema 和实际契约测试为准。滚动更新文档与固定源码存在差异时，需以该开发基线核对，不能据文档示例假定支持 Chat Completions provider。

文档说明 app-server 通过 thread/turn 和事件驱动产品集成，动态工具需实验接口 opt-in；结构输出 schema 不替代业务校验。MEDIC 因此选择稳定生命周期 + MCP 为初版基础，将实验接口隔离在 adapter 内。

## P0 实测与仍未验证的边界

- 固定源码构建成功，11 个核心生成 schema 契约一致；使用自建二进制完成真实 MCP 工具调用、结构化输出、跨进程恢复、4 thread 并发及中断。未借用桌面二进制作为源码构建证明。
- 客户 Chat/Responses 扁平工具、SSE、JSON 样本验证成功；原生 namespace 工具实测失败。当前由 `medic/p0/namespace_gateway.py` 完成最小转换，未修改 Rust 内核。
- 只读核对了客户模型、矩阵、demo 和 policy；公开记录只有统计、哈希与差异，原始表和行为锚点没有进入仓库。
- 最大上下文、并发配额上限、视觉、并行工具调用、压缩、原生强制 JSON Schema、生产权限隔离和评分质量仍未验收。
- 未运行旧 Python 初版测试；未实现 P1–P5 的业务 runtime、正式导入发布或员工评估。业务版本与质量门槛仍需签收。

完整证据索引与复跑方法见 [P0 验收记录](P0_REPORT.md) 和 [P0 README](p0/README.md)。
