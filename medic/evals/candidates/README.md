# 候选补丁历史

本目录保留 v16 时期保存的候选补丁及当时的验证记录。2026-10-28 已将引用选择接入 v17 系列开发，并完成 PostgreSQL 回归；原补丁不再代表当前代码。当前真实运行和剩余问题见[开发状态](../../DEVELOPMENT_STATUS.md)。补丁不由应用启动或自动加载。

## evidence-selection-v17.patch

相对于已归档 v16 的统一补丁，包含新工具 `assessment_submit_by_evidence_id`、按 schema 定位的整段证据解析、v7 角色包、相应规划/争议启用条件、候选构建清单和 9 项契约检查。只在独立 v16 源码副本中应用，不能在正在服务的 v16 目录直接应用；修改执行文件会触发版本完整性门禁。

模型必须显式提交 `{"evidence_id":"..."}`，选择的是该任务冻结的完整原文块。服务端只展开 EvidenceRef 类型字段，然后调用原有结果校验和持久化入口。不猜测引用、不修补旧接口的改写引文、不增加额度、不替代模型的判断。旧角色包与网页默认 v5 不变。mailbox 仍使用原来的完整引用协议。

9 项 SQLite Store/ToolService 检查通过，无模型调用；不构成 PostgreSQL、第三轮全链、真实模型或完整 C 评估验收。开发时保留了缺少新工具的红测试，以及首次绿测中的测试快照读取错误；修正测试后 9 项通过。核验摘要见 `evidence-selection-v17.json`。该补丁仅保存当前进度，不代表可发布候选。

后续须补齐多材料越界、所有结果 schema/第三轮与实际工具预算覆盖、历史角色包兼容回归、PostgreSQL 检查，再冻结前瞻真实模型探针。原 v16 失败运行保持失败，不能用候选解析重写其结果或预算。

## 2026-10-28：引用选择边界测试扩展

`evidence-selection-v17-boundaries.patch` 在原候选上增加两个测试模块；原补丁和原 2026-10-23 记录保持不变。扩展验证普通材料不得选择另一材料的证据、旧 prompt bundle 不获得新工具、工具与补正预算不增加、结果不可变、完整多材料合成图、定向第三轮返工，以及旧 blocking 争议不能遗漏。解析器仍只展开 schema 指定的引用，不修改模型的事实陈述、理由或档位。

引用选择批次 33 项 SQLite 检查通过，24 项 PostgreSQL 同构检查因未配置 `MEDIC_TEST_POSTGRES_DSN` 跳过。另一个受影响核心批次 83 项通过、无跳过；包括复用用例，不将两个批次累加为独立测试数量。新增模型调用为零，尚未验证真实模型、完整 C、PostgreSQL 或质量收益。详见 `evidence-selection-v17-boundaries.json`。

使用 Python 3.12+、已安装 requirements 的独立环境，在新的源码副本顺序应用两个补丁。以下命令从仓库根目录开始，保留主目录 v16 文件不变：

```bash
MEDIC_CANDIDATE_PYTHON="$PWD/medic/.venv/bin/python"
MEDIC_CANDIDATE_ROOT="$(mktemp -d)"
git archive cb176c192 medic | tar -x -C "$MEDIC_CANDIDATE_ROOT"
git apply --unsafe-paths --directory="$MEDIC_CANDIDATE_ROOT" "$PWD/medic/evals/candidates/evidence-selection-v17.patch"
git apply --unsafe-paths --directory="$MEDIC_CANDIDATE_ROOT" "$PWD/medic/evals/candidates/evidence-selection-v17-boundaries.patch"
cd "$MEDIC_CANDIDATE_ROOT"
"$MEDIC_CANDIDATE_PYTHON" -m unittest medic.tests.test_evidence_selection medic.tests.test_evidence_selection_boundaries medic.tests.test_evidence_selection_postgres -v
"$MEDIC_CANDIDATE_PYTHON" -m unittest medic.tests.test_runtime medic.tests.test_aggregation medic.tests.test_rework medic.tests.test_receipt_recovery medic.tests.test_build medic.tests.test_compact_plan medic.tests.test_mailbox medic.tests.test_worker -q
```

PostgreSQL 入口仅接受显式以 `_test` 结尾的数据库，复用 `test_postgres.database()` 的准入规则。未设置测试连接时只会明确跳过，不会连接试用/生产库。临时源码目录由操作者保留或在不再需要时清理；历史运行只在各自归档版本重放。
