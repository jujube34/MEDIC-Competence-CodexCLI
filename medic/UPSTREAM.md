# Codex 复用依据与版本记录

检查日期：2026-10-16。这里记录源码调查，不表示已构建 Codex 或完成客户 DeepSeek 联调。

## 仓库与基线

| 项目              | 值                                                                            |
| ----------------- | ----------------------------------------------------------------------------- |
| 上游              | [openai/codex](https://github.com/openai/codex)                               |
| 用户已有公开 fork | [jujube34/codex-MedSci](https://github.com/jujube34/codex-MedSci)             |
| 本次方案分支      | `medic/agent-runtime-plan`                                                    |
| 方案基线          | `5b1d6560181680f95cde95c14ed042acc02248ed`，用户 fork 的 `main`（2026-09-14） |
| 调查时上游 main   | `6ae8b8aa7c608730d4315d59c5c081897ae02780`（2026-10-16 查询）                 |
| 新增内容范围      | `medic/`；不修改上游代码，不更新用户 fork 的 `main`                           |
| 许可              | 保留仓库 LICENSE/NOTICE；Rust workspace 标注 Apache-2.0                       |

GitHub fork 请求返回了用户已有 fork，因此复用该仓库，没有另建同名副本或重命名它。本地先获取上游对象，再获取用户 fork 的 main，当前分支以用户 fork 的原有提交为起点；`origin` 指向用户 fork，`upstream` 指向官方仓库。使用 shallow / partial clone 节省传输，当前工作树已检出；需要完整历史时再按需 fetch。

现有 fork 的基线用于保证本次改动仅为计划文档，不代表生产版本已选定。P0 应比较固定发行版与更新的上游，完成兼容测试后写入部署锁定清单。禁止以浮动 `main` 直接部署。

## 已从固定基线源码确认的能力

以下相对路径均位于仓库根目录。链接固定到本次基线 SHA，避免以后行号漂移。

| 能力                      | 源码依据                                                                                                                                                                                                                                                       | 对方案的含义                                                                                         |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 自定义模型 provider       | [model-provider-info/src/lib.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/model-provider-info/src/lib.rs#L99) 定义 base_url、env_key、requires_openai_auth、supports_websockets                         | 可连接内网自定义服务，无需修改默认 OpenAI provider 或依赖个人 ChatGPT 登录                           |
| 只支持 Responses wire API | [同文件 WireApi](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/model-provider-info/src/lib.rs#L61) 只有 Responses，解析 chat 时明确报错                                                                      | 客户 Chat Completions 示例不直接兼容，必须验证 Responses 端点或适配网关                              |
| app-server stdio          | [cli/src/main.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/cli/src/main.rs#L552) 定义 app-server、listen 和 stdio                                                                                       | 外围服务可通过子进程驱动，不需要嵌入 Rust 内核                                                       |
| thread / turn 生命周期    | [protocol/common.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/app-server-protocol/src/protocol/common.rs#L507) 包含 initialize、thread/start、thread/resume、turn/start、turn/interrupt、turn/completed | runtime 可管理独立角色会话、事件和取消；业务任务状态仍自行持久化                                     |
| 角色指令与配置            | [ThreadStartParams.ts](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/app-server-protocol/schema/typescript/v2/ThreadStartParams.ts) 包含 modelProvider、cwd、baseInstructions、developerInstructions、config | 可注入窄职责上下文；这些字段本身不是业务授权系统                                                     |
| 每回合结构化结果          | [TurnStartParams.ts](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/app-server-protocol/schema/typescript/v2/TurnStartParams.ts) 定义 outputSchema                                                            | 可约束角色输出形状；引用真实性、评分上限和权限仍由 runtime 校验                                      |
| 动态工具是实验接口        | [protocol/v2/thread.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L138) 对 dynamic_tools 标注 experimental                                                 | 第一版优先 MCP；不能因为普通生成 TS 中没有该字段就断言源码不支持，也不能未经实验协议 opt-in 直接使用 |
| 原生 Agent 协作           | [multi_agents.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/core/src/tools/handlers/multi_agents.rs) 组织 spawn/send_input/wait/close 等 handlers                                                        | 有可进一步复用的基础，但不能替代业务权限、持久任务图、租约与审计；DeepSeek 实际调用仍待测            |
| 原生 goal API             | [protocol/common.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/app-server-protocol/src/protocol/common.rs#L616) 包含 thread/goal/set/get/clear                                                           | 能辅助跟踪会话目标；MEDIC 的评估放行必须另有独立 Judge 与程序 gate                                   |
| exec JSONL 与 schema      | [exec/src/cli.rs](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/codex-rs/exec/src/cli.rs#L47) 定义 output-schema 和 json                                                                                              | 用于小规模接入试验与回归，不把一串 exec 脚本当作完整生产 runtime                                     |
| TypeScript SDK            | [sdk/typescript/README.md](https://github.com/jujube34/codex-MedSci/blob/5b1d6560181680f95cde95c14ed042acc02248ed/sdk/typescript/README.md) 描述 CLI 子进程、JSONL、流式、恢复与结构输出                                                                       | 若后续采用 TS 可选用；Python 服务直接接 app-server 无须复制 SDK 内部实现                             |

另外已读取当前上游 `6ae8b8aa7c608730d4315d59c5c081897ae02780` 的 `codex-rs/model-provider-info/src/lib.rs`，同样只支持 Responses 并拒绝 chat。因此不是仅因旧 fork 才存在此兼容问题。

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

已读取 [Codex App Server](https://learn.chatgpt.com/docs/app-server) 和 [Advanced Configuration](https://learn.chatgpt.com/docs/config-file/config-advanced)。官方文档用于理解生命周期、配置与接口类别；实现时以固定源码的生成 schema 和实际契约测试为准。滚动更新文档可能与旧 fork 不一致，例如配置说明存在提到旧 Chat Completions provider 的段落，但上述两版源码均已拒绝 chat。

文档说明 app-server 通过 thread/turn 和事件驱动产品集成，动态工具需实验接口 opt-in；结构输出 schema 不替代业务校验。MEDIC 因此选择稳定生命周期 + MCP 为初版基础，将实验接口隔离在 adapter 内。

## 尚未验证

- 未编译/启动此固定源码版本，也未将本机 Codex 桌面运行时的能力等同于开源版本的能力。
- 未调用客户 API；没有测试真实 tools、SSE、上下文、视觉、JSON Schema、并发配额或评分质量。
- 未运行旧 Python 初版测试；计划中的旧 mock 结果来自其本地 README/VERIFY 记录。
- 未把原始矩阵或完整行为锚点发布到仓库；其导入和内容一致性检查属于 P0。
- 未修改 Codex Rust 内核、业务实现、客户原始文件或用户 fork 的 main。

第一阶段的成功证据应包括：固定二进制版本、provider profile、合成用例输入、脱敏协议事件、完整工具往返和明确通过/失败记录。
