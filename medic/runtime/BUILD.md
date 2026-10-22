# 应用发布与运行身份

当前服务为 `medic-runtime-p4-v16`，106 文件清单 hash `bdc3c02d9c1c79b4c68302c0b09c6ba0258da02502f32ca9bf3f0fdb0e2e89fa`。该版增加当前任务/租约的只读提交回执查询及补正提示，完整评分校验、最终回执精确匹配和预算不变。真实受控故障探针与发布验证见 [v16 记录](../evals/results/p4-receipt-recovery-verification.json)；随后完整范围评估因引用校验拒绝而失败，不能据此认定通过。以下版本段落为历史发布记录。

从 `medic-runtime-p2-v3` 起，新 run 在数据库 `run_builds` 中冻结应用身份，旧运行不追溯补写。冻结内容包括：

- 发布版本和 `build-manifest.json` 的规范化 SHA-256。
- Python 实现、版本、平台、解释器文件 SHA-256，以及列出的执行依赖版本。

历史发布 `medic-runtime-p3-v7` 继承 v6 数据库位置准入和 API 环境变量配置入口，修复网关完成通知与结算时序、SQLite 只读轮询的写锁竞争。p2-v3、p3-v1 至 p3-v6 的源码归档在本地 `medic/artifacts/releases/`，旧清单在 `deploy/releases/`；已有真实运行保持各自的原身份。不要用当前发布直接恢复旧版本 run。
- 清单覆盖 adapters/apps/domain/ingestion/p0/prompts/runtime/tools 的 Python、数据库 SQL、prompt/schema JSON、根包 Python 与 requirements 文件。测试、探针脚本、文档、私有运行和客户数据不进入清单。v3 同时记录 API 依赖版本；API 启动校验当前清单。
- v4 将 `apps/web` 下的 HTML、CSS、JavaScript 也纳入清单。静态页面与 API 一起发布，不能在旧服务上直接替换新页面再声称是原冻结版本。
- v6 清单包含 87 个执行/资源文件，规范化 hash 为 `ef8a27fd2e8b2857aa279d361ed60a0d87dc59acf63eb503fb8075b51681ecc9`。242 项完整回归通过；数据库入口新增 9 项测试，并完成真实 PostgreSQL 的环境变量启动、HTTP 上传/创建与重启保留核验。细节见 [P3 进展](../P3_PROGRESS.md)。
- v7 清单仍包含 87 个文件，hash 为 `d331b70cc688f72e011e10ea88d132d0170f55681a3f58ff29edd24c44f9f9fe`；252 项回归（83 项真实 PostgreSQL）及新的 HTTP 六角色真实模型探针通过。与 v6 失败探针分开记录，验证范围见 [P3 进展](../P3_PROGRESS.md)。

进程启动时记录文件身份。每次验证同时比对当前文件、发布清单和进程启动身份；增加、删除、修改受管文件均拒绝执行，即使替换清单也不能让旧进程认可新代码。父 worker 与独立 MCP 子进程都执行校验，数据库事务绑定同一 run 的身份。任务 context hash 包含此身份，worker 协议和报告记录实际身份；报告由数据库值覆盖调用者提供的身份。

新建、任务租约、工具授权、上游请求预留、恢复、返工规划和发布都检查身份。不匹配时不会发送新的模型请求、改变旧结果或重置预算。取消、历史只读和已发请求的用量结算不受阻断，便于安全结束与保留账本。

同一个 run 仅允许相同代码和执行环境恢复；不同 Python/依赖版本亦会拒绝。正常升级需要保留旧的只读部署和对应依赖环境供旧 run 完成，新部署创建新 run。已经发布的历史 run 可跨版本只读查看；完整 build 重验需要对应发布的代码与清单，不能把“当前版本不支持核验”当成历史执行已验证。没有原始 build 身份的历史 run 返回 `legacy_run_has_no_frozen_build`，不能自动认作当前版本；需要新建评估，原预算与事件原样保留。

开发新发布时先变更 `build.VERSION`，在新的发布目录生成清单：`python -m medic.evals.freeze_build`。命令拒绝覆盖已有清单；验证完毕后固定清单和制品，发布后不再原地修改。当前新建夹具为 `single_dimension_current.json`、`multi_material_current.json`。运行期间不安装或更新依赖。

这些检查用于发现部署混用和意外修改。清单不是签名，依赖版本记录不是完整 wheel/动态库哈希锁；拥有部署目录或数据库写权限的管理员不属于该机制防护边界。生产仍需只读发布目录、镜像/依赖制品锁、权限隔离和客户环境验收，不能仅凭本地版本测试声称已上线。

2026-10-23 当前发布更新为 `medic-runtime-p3-v11`，96 个执行/资源文件，hash `1e038fee8e41b6e93e9d65d846ed0f730fa8e1c4db7f9e2d351ad797be2a7261`。v8–v11 均已分别归档，不覆盖历史版本。v11 只改动五个前端文件和发布版本号，44 项受影响回归通过；完整验证范围见 [P3 进展](../P3_PROGRESS.md)。

2026-10-23 当前发布更新为 `medic-runtime-p3-v12`，102 文件清单 hash `11ee4d2547dc46e28ff851ccaa69375e6712704e8ae51f74db9771897d28cc25`。Office 转换器以独立固定制品归档，源文件 hash 常量与验证代码纳入应用清单。完整候选 302 项回归与最终仅前端去重后 48 项受影响回归分别绑定各自构建，见 [v12 记录](../evals/results/p3-v12-contracts-verification.json)。旧运行保持原身份；派生 Office 预览是当前展示制品，不重写原评估执行版本。

2026-10-23 发布 `medic-runtime-p4-v15`，106 文件清单 hash `a683d113996b06600e87bfe3e2b121282e9b2854fe899b0f01728968517d990b`。新增 v6 角色包的显式紧凑任务图派发；旧 v5 工具与定向返工保持。两次真实全范围规划与 SQLite/PostgreSQL 事务核验通过，评分任务未执行，完整 C 预算仍待验证。应用 API/队列已升级，前端文件保持 v14；默认 API 创建的输入仍使用 v5，实验显式选用 v6。v14 及 v15 均有独立源码归档，旧运行不重绑。详见 [P4 进展](../P4_PROGRESS.md)。
