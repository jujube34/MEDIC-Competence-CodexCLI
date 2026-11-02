# Lite 运行模式

`MEDIC_LITE` 是 v18 精简分支的运行档位，用于优先跑通完整 17 项能力测评，而不是做生产验收。

## 保留

- 完整任务图：Coordinator → Evaluator / Challenger Scan → Challenger → Reevaluator → Aggregator → Judge
- `assessment_submit_by_evidence_id` 整段证据选择与服务端校验
- policy 约束：`grade=null`、策略上限、`human_only` 不交给 AI
- 租约、幂等完成回执、不可变结果、预算/并发准入
- 员工/经理授权范围与原始快照 hash
- 网页 Agent 运行日志

## 关闭

- mailbox 跨角色问答（`runtime_read_messages` / `runtime_post_message`）
- 第三轮 business rework（Judge 的 `rework` 结论不再触发自动返工，按人工处理）
- 热路径上的逐文件 build 校验
- OCR（`ingestion/ocr.py`）和 PDF 扫描页识别
- 独立解析子进程、Windows Job / POSIX 进程组资源限制（lite 直接在 API 进程内解析）
- PDF 原页预览、Office 转换预览和 `preview.js` 相关路由（lite 返回 `preview_not_supported`）
- 上传日配额、跨进程文件锁和准入 SQLite 表（lite 只保留临时文件和大小检查）

## 启动

```bash
export MEDIC_LITE=1
export MEDIC_BUILD_VERIFY=off          # 仅本地开发；生产不要设置

# API
python -m medic.apps.api \
  --database medic/var/lite/runtime.sqlite3 \
  --vault medic/var/lite/vault \
  --port 8765

# 队列服务（另一个终端）
export MEDIC_LITE_DATABASE=medic/var/lite/runtime.sqlite3
python -m medic.runtime.service \
  --database-env MEDIC_LITE_DATABASE \
  --binary /path/to/codex \
  --api-example /path/to/api-example.txt \
  --output medic/var/lite/queue \
  --poll-seconds 1
```

`MEDIC_BUILD_VERIFY=off` 只影响本进程的构建清单校验；需要审计时去掉该变量，并确保 `runtime/build-manifest.json` 与当前源码一致。

Lite 的解析在 API 进程内执行，只适用于受信任的合成材料。不要用 lite 模式接收客户原始材料或不可信上传。

## 当前边界

- 本轮只完成第一批精简；PostgreSQL 双后端、Office/OCR/预览、上传配额、旧 prompt bundle、A/B/C 对照工具仍在代码中。
- 暂停的 v17.4 运行保留为 cancelled，不追认为通过。
- Lite 模式尚未重新执行完整 17 项真实模型运行。
