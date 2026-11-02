# v18 精简架构计划

更新：2026-10-28。基线为 `cb176c192`，当前分支 `refactor/lite-architecture`。v17.4 的完整 17 项试运行已暂停并保留为 cancelled；本计划按“对跑通完整 17 项测评的收益”排序，而不是按代码整洁度排序。

## 原则

保留结果可信度所需的核心机制：

- `assessment_submit_by_evidence_id` 整段证据选择 + 服务端展开和校验
- policy 约束：`grade=null`、策略上限、`human_only` 不交给 AI
- 租约、幂等完成回执、不可变结果
- 预算、并发与授权范围
- 原始快照与内容 hash

优先删除或默认关闭：只服务于审计/历史回放、当前 DOCX 场景不使用、或存在重复实现且不影响结果可信度的机制。

## 当前状态（2026-11-02）

- 阶段 1 已完成：lite 直接在 API 进程内解析，关闭 OCR、解析子进程/进程组限制、预览路由和上传配额锁。
- 阶段 2 部分完成：移除 compact plan、evidence selection、mailbox 的版本白名单；旧 prompt bundle 文件仍在，供 legacy 测试和只读重放。
- 完整测试：600 项通过，157 项按环境跳过，0 失败。
- 阶段 1 的 PostgreSQL 部分只做到“默认 SQLite”；PG 兼容代码仍在，留到后续处理。

## 受益排序

| 排名 | 事项 | 收益 | 风险 | 验收 |
|---|---|---|---|---|
| 0 | 已完成：build 校验缓存 + 可选关闭；v8 移除旧引文提交；`MEDIC_LITE` 关闭 mailbox/第三轮 | 直接消除两类主要失败点，降低迭代摩擦 | 低 | 597 项测试通过，157 跳过 |
| 1 | ✅ 关闭/移除 DOCX 场景不用的 Office、PDF 预览、OCR、进程组资源采样 | 代码面和依赖显著减少；减少平台差异 | 中 | DOCX 上传/解析/证据/评估全链路通过 |
| 2 | ✅ 上传配额、跨进程文件锁、准入 SQLite 表改为 lite 简单检查 | 去掉与当前单机试运行无关的配额状态机 | 低 | 上传 2 份 DOCX 并创建评估 |
| 3 | ◐ PostgreSQL 改为可选后端，默认只跑 SQLite（默认已完成，代码清理待做） | 去掉双后端兼容代码和 148 项 PG 测试对主流程的干扰 | 中 | 默认测试全绿；PG 测试显式开关 |
| 4 | ◐ 归档 v2–v7 prompt bundle，运行时只加载 v8（版本分支已移除，文件归档待做） | 去掉多版本条件分支和旧 schema 兼容 | 中 | 新 run 只用 v8；旧 run 仅归档重放 |
| 5 | 把 A/B/C 对照、baseline A/B、离线指标移出运行时包 | 主运行时不再携带研究工具和实验身份逻辑 | 低 | runtime/API 不 import research 模块 |
| 6 | 合并 `events` / `app_events` / `task_history` 等重复审计表 | 减少状态同步和测试面 | 高 | 事件流、任务恢复、终态归档仍一致 |
| 7 | 简化人工复核状态机和四层幂等 | 减少 UI/API 分支和重复键 | 中 | 创建、复核、定稿各一条幂等路径 |
| 8 | 简化网页：保留“运行日志 + 结果 + 人工备注”，移除暂不用的管理视图 | 减少前端代码和状态 | 低 | 员工/经理两条主路径可用 |
| 9 | 用真实模型重新执行完整 17 项，并与暂停的 v17.4 运行对比 | 验证精简没有破坏完整流程 | 高 | 226/226 节点完成或明确失败，产生报告 |

## 分阶段执行

### 阶段 1：DOCX 主路径（排名 1–3）

1. 在 lite 模式下只保留 `vault.py`、`parser.py`、`ooxml.py` 的文本提取。
2. 对 `ingestion/office*.py`、`ocr.py`、`preview*.py`、`posix.py`、`bounded.py` 做以下之一：
   - 从 lite 入口移除；
   - 或保留为显式可选依赖，默认不 import。
3. API 的 `/materials/{id}/preview` 与 Office 预览路由在 lite 模式返回固定错误码。
4. 上传准入改为简单的类型/大小/并发检查；不再维护 24 小时配额表和跨进程锁。
5. 默认数据库只启动 SQLite；PostgreSQL 通过显式 `--database-env` 启用。

**验收**：两份合成 DOCX 上传、解析、块读取、创建评估、运行 17 项任务图、网页日志可见。

### 阶段 2：单一 prompt 与运行时边界（排名 4–5）

1. 新建 `prompts/archive/`，移入 v2–v7；`prompts/manifest.json` 只保留 `medic-p2-roles-v8`。
2. 删除 `tools/service.py`、`runtime/planning.py`、`runtime/mailbox.py`、`runtime/admission.py` 中的多版本判断，只保留 v8 路径。
3. 旧 run 只通过归档代码/归档 prompt 重放，不再由当前运行时加载。
4. 将 `evals/baseline_a.py`、`baseline_b.py`、`comparison*.py`、`metrics.py`、`runtime_measurements.py` 移到独立的 `research/` 目录或仓库。
5. 主运行时、API、测试不得 import research 模块。

**验收**：`prompts/manifest.json` 只有一个版本；grep 不到 v2–v7 分支；runtime/API 无 research import。

### 阶段 3：审计与状态机（排名 6–8）

1. 评估 `events`、`app_events`、`task_history`、`task_cycles` 的重叠字段，保留一份运行时事件流和一份应用审计流。
2. 如果不需要第三轮历史回放，删除 `task_history`/`task_cycles` 或仅在归档工具中使用。
3. 人工复核状态收敛为 `pending → ready → finalized`；保留 AI gate 原值。
4. 幂等只保留服务端 `Idempotency-Key` 或 runtime `creation_key` 其中一层。
5. 网页保留 Agent 日志、结果、人工备注；移除未使用的管理/对照视图。

**验收**：创建/重复创建、复核并发、定稿、取消、恢复各一条测试通过。

### 阶段 4：完整 17 项验证（排名 9）

1. 使用精简后的运行时冻结新版本。
2. 同一两份合成材料、17 项能力、226 节点，真实 DeepSeek。
3. 预算仍为 2000 请求 / 1800 万 tokens / 并发 3；墙钟按实际需要单独记录。
4. 如果失败，保留原运行、错误码和用量；不补写结果、不扩大历史预算。
5. 与暂停的 v17.4 运行（96/226, 505 请求, 4,828,175 tokens）对比节点进度和失败原因。

**验收**：产生完整报告；或明确记录失败阶段与原因，并据此决定下一轮修正。

## 明确的非目标

- 不追求生产 SSO、组织同步、客户标准签收、专家质量校准。
- 不把单次技术成功当作业务质量通过。
- 不删除证据校验、policy 约束、租约/预算/授权，以换取“跑得快”。
- 不重写历史失败运行或在旧预算上追认成功。

## 回滚

每个阶段独立提交；保留 v17.4 冻结部署和 pause 运行记录。若精简导致完整任务图无法完成，回退到该阶段之前的提交，而不是放宽校验。
