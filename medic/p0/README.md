# P0 基线与可行性

本目录只实现基线校验、只读资产审计和合成协议探针。所有通过项都区分本地契约测试与真实模型测试。最终结论见 [P0_REPORT.md](../P0_REPORT.md)。

从仓库根目录运行，Python 3.11+：

```powershell
python -m pip install -r medic/requirements-p0.txt
python -m unittest discover -s medic/tests -v
python -m medic.p0 assets --root '<客户资料目录>' --out medic/artifacts/p0/assets.json
python -m medic.p0 build --cargo '<cargo可执行文件>' --directory medic/artifacts/p0
```

`build` 使用 Rust 1.95.0、`cargo build --locked`，生成固定源码的 app-server JSON Schema，对比关键生命周期契约，并记录二进制、Cargo.lock 与 schema 哈希。先安装 `rustup toolchain install 1.95.0 --profile minimal`。Windows 需 MSVC 构建工具。当前构建为 debug 可行性版本，不是生产发行包。

通过独立环境变量配置客户端点；值由客户秘密管理系统注入，不写到命令行、公开文件或全局 Codex 配置：

```powershell
# 必需：MEDIC_PROVIDER_BASE_URL、MEDIC_PROVIDER_API_KEY
# 可选：MEDIC_PROVIDER_MODEL，默认 deepseek-v4-flash
python -m medic.p0 provider --out medic/artifacts/p0/provider.json
python -m medic.p0 appserver --binary codex-rs/target/debug/codex.exe --namespace-gateway --threads 4 --out medic/artifacts/p0/appserver.json
python -m medic.p0 gate --directory medic/artifacts/p0
```

也支持 `provider`/`appserver --api-example '<本地私有curl示例.txt>'`：仅提取示例的 URL、Bearer token 和模型名，不执行示例代码。不得把该文件提交到 Git。Unix 构建的二进制名称为 `codex`。

退出码 0 表示所执行检查通过，2 表示未通过；配置缺失、输入文件错误会直接失败。当前客户原生 namespace 探针预期失败（`provider` 返回 2），必须通过 `--namespace-gateway` 路线继续验证。`gate` 必须同时看到基线构建、数据审计、真实 Responses 和同一二进制/上游端点/模型的 app-server 生命周期证据。单独运行 `schema` 或本地单元测试不能解锁 P0。

## 产物与数据边界

- `medic/artifacts/` 已被忽略：保存完整生成 schema、构建日志、当地运行证据、隔离的 CODEX_HOME、合成会话与 MCP receipt。目录内的 provider 配置包含客户地址；凭据只在进程环境中传递。
- `assets.json` 只包含来源哈希、能力编码、统计、字段级差异与冲突。解析使用只读模式，不修改工作簿，不复制客户原文或岗位名。
- `provider.json` 包含 HTTP 状态、事件类型、请求/响应哈希、用量、耗时和结果；不保存 Authorization、完整请求或模型隐藏推理。
- `baseline.json` 记录源码和二进制身份；`appserver.json` 记录独立进程恢复、并发标记隔离、MCP 实际执行与中断结果。
- 公开仓库只保存审阅后的脱敏结果摘要；新增真实 trace 不自动加入 Git。报告不是客户签收记录。

## 验证边界

探针仅发送合成输入。Responses 工具回传携带运行时生成的随机 receipt，最后答复必须准确引用它，防止把“模型说调用了工具”当作工具闭环。恢复测试关闭原进程并启动新进程；并发测试要求至少两个活动 turn 重叠，分别核对随机 marker。取消在流式输出开始后发出，必须收到 `interrupted`，网关同时确认下游断开与上游连接关闭，不能把 ACK 当作完成。

工具参数在执行侧校验；未知工具、未知证据、附加参数、非法 JSON 被拒绝。HTTP 不跟随重定向，不读取代理环境变量，不自动重试；401/403、429/5xx、断流、超时均不会降级成 demo。直接探针每次最多 40 个请求，单请求输出上限 768 tokens（部分短用例更低），工具循环有硬上限。app-server 探针的网关最多 40 请求、4 并发、2048 输出 tokens；缺失用量按请求字节数加输出限额保守估算，不记零。生产重试与预算属于后续 runtime。

JSON Schema 请求成功且输出校验通过只证明这些样本可用，不证明服务端原生强制 schema。18k 字符合成材料成功不等于测得最大上下文窗口。视觉、并行工具调用、压缩及生产权限隔离未验证时不得启用。

app-server 在独立 CODEX_HOME 与空工作目录运行，关闭 shell、多 Agent、web search 等功能；这不是容器或操作系统 ACL 的替代品。正式员工材料必须等外部文件与网络边界验收后才能引入。P0 通过只准入 P1 合成闭环，不准许发布员工结果。

## 最小网关范围

`namespace_gateway.py` 仅处理已复现的格式缺口：将 namespace 内的 function 工具转换成具有唯一别名的扁平工具，并在输出 item、终态和多轮历史中还原 namespace/name。保持 system/developer/user/tool 的类型、顺序与 call ID；不把工具结果当作用户消息，不转换为 Chat Completions。

仅绑定随机 loopback 端口，使用每次运行新生成的访问令牌；上游固定为传入的客户地址与模型。支持范围之外的 custom/tool_search/deferred 工具、stateful previous_response_id 和非 Responses 路径显式拒绝。中断关闭上游 HTTP 连接，但不宣称客户推理服务停止计费。客户端点原生修复 namespace 后可移除该适配层，重跑同一契约套件。
