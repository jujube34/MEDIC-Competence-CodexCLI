# 本地应用 API

应用层接入上传、证据读取、评估创建、结果、人工复核、定稿和审计记录。只监听回环地址，执行仍限定合成员工和合成标准；客户候选标准可以由可信操作者注册展示，但不能创建正式评估。当前本地 Bearer 身份用于工程验证，尚未对接客户 SSO。

## 启动

在仓库根目录使用已安装 `medic/requirements-api.txt` 的独立 Python 环境：

```powershell
python -m medic.apps.api.bootstrap medic/var/local-api
python -m medic.apps.api --database medic/var/local-api/runtime.sqlite3 --vault medic/var/local-api/vault --port 8765
```

初始化要求全新私有目录，生成虚构员工及员工、上级、总监三个账号。本地访问 token 保存在该目录 `local-access.json`，不会打印到终端；默认一天失效。该文件依赖本机目录 ACL 保护，不是生产密钥管理实现。操作者可用 `ApplicationDB.provision_user` 签发新 token，设置 `app_users.active=0` 或删除相应 `app_tokens` 行撤销；下一次服务端授权即失效。HTTP 不提供自行创建身份、变更岗位或授予范围的入口。

PostgreSQL 使用环境变量传入 URI，避免连接凭据出现在进程参数中；`--database` 与 `--database-env` 必须且只能选择一个：

```powershell
# MEDIC_API_DB 由本地配置设置为 postgresql://... 或 postgres://... URI。
medic/.venv/Scripts/python.exe -m medic.apps.api --database-env MEDIC_API_DB --vault medic/var/pg-api/vault --port 8765
```

`medic-runtime-p3-v6` 在任何文件写入前区分 PostgreSQL URI 和 SQLite 文件路径。`host=... dbname=...` 等 libpq 关键字连接串、其它 URI scheme、空值及含首尾空白的配置会被明确拒绝，不再被当成本地文件。SQLite 支持带空格/中文的完整路径；含 `=` 的文件名应使用明确的目录前缀。PostgreSQL 连接失败也不会降级为 SQLite。启动失败只输出错误类型与允许公开的错误码，不回显连接内容。

API 请求通过 `Authorization: Bearer <token>` 认证。角色、员工、岗位、租户来自服务端账号和目录；自报角色头、请求体中的未知字段不产生授权。员工只能访问本人，上级/总监必须有明确员工范围；人工复核还要求 `can_review`，禁止自评定稿，管理员角色本身不产生复核权限。

## 接口

浏览器访问同一服务的 `/` 即可打开[三类角色网页](../web/README.md)。页面资源由本地服务提供，使用严格 CSP；登录 token 只保留在页面内存中。

| 方法与路径 | 行为 |
| --- | --- |
| GET `/health`、`/me` | 健康与当前身份 |
| GET `/employees`、`/catalogs` | 授权员工目录与本租户标准；候选状态明确展示 |
| POST `/employees/{employee}/materials?filename=...&period=...` | 二进制文件请求体；最多 25 MiB；受限解析后重新检查授权 |
| GET `/employees/{employee}/materials` | 该员工材料快照列表 |
| GET `/materials/{snapshot}` | 质量状态、hash、解析版本与问题 |
| GET `/materials/{snapshot}/blocks?offset=0&limit=50` | 原文、定位、引用 hash；分页最多 100 块 |
| GET `/materials/{snapshot}/blocks/{evidence_id}` | 按精确证据定位原文块，继续执行员工范围校验 |
| GET `/materials/{snapshot}/source` | 校验 hash 后以附件下载原文件 |
| GET `/materials/{snapshot}/preview?page=1&width=1200&evidence_id=...` | PDF 原页 PNG；可选冻结证据 ID 显示红框；认证与渲染后授权复查 |
| POST `/assessments` | 冻结输入并创建持久 runtime run，不在 HTTP 中执行模型 |
| GET `/assessments`、`/assessments/{id}` | 范围内列表及 AI/推荐值/人工结果分栏数据 |
| POST `/assessments/{id}/reviews` | 乐观版本检查，保存人工判断与理由、引用、补充材料 |
| POST `/assessments/{id}/finalize` | 完整覆盖及风险核验通过后定稿合成复核记录 |
| GET `/assessments/{id}/history` | 追加式复核与定稿审计记录 |

创建、复核和定稿要求 `Idempotency-Key`，长度 8–128，允许字母、数字、`_.:-`。相同 key/内容返回原收据；改内容返回 409。创建先持久化应用意图，再用 assessment ID 幂等创建 runtime；进程在两步间退出后，重发相同请求继续连接原 run，不新建预算。

创建请求包含 `employee_ref`、`period`、`registry_id`、`material_ids`、`competency_scope`。材料必须属于该员工/周期且 `ready`，评分范围必须来自同一标准。岗位推荐值单独冻结，不进入 Agent input。队列由现有 runtime 命令执行，例如将 `MEDIC_API_DB` 设为私有 SQLite 完整路径或 PostgreSQL DSN 后：

```powershell
python -m medic.runtime --resume-run RUN_ID --database-env MEDIC_API_DB --binary CODEX_BINARY --api-example PRIVATE_API_EXAMPLE --output PRIVATE_RUN_DIRECTORY
```

不得以该命令创建第二个 run 替代已有队列。应用和 worker 必须采用相同冻结版本与依赖环境。自动处理新建任务可启动[持续调度服务](../../runtime/SERVICE.md)；当前限定 Windows，每个数据库同时处理一个评估，生产部署编排仍待验收。

## v14 目录展示元数据

`GET /catalogs` 可返回 `job_profiles`（岗位键映射到 `family`、`track`、`title`、`level`）和 `competency_names`（能力代码映射到名称）。非空映射必须完整覆盖该目录已有岗位或能力，字段不得为空白。旧目录允许缺少元数据；注册时省略空映射以保留旧 ID，新元数据产生新内容地址，不覆盖旧目录。

客户分类从原始矩阵明细读取；网页只提供三级筛选和标准浏览，HTTP 不提供注册、编辑或发布目录的写接口。候选仍为 `awaiting_business_signoff`，不能创建正式评估。展示元数据不进入 Agent input；历史岗位推荐值继续使用评估创建时冻结的版本。

## 人工判断

复核请求带 `expected_revision` 与 `decisions`。每项指定能力、状态、grade、理由，以及精确原文引用或明确的人工观察依据。`confirmed` 对应与 AI 同档；`adjusted` 对应已有 AI 档位的变更；`assessed` 用于 AI 没有档位时的独立人工判断；`insufficient_evidence`、`not_applicable` 保持 null。人工可以给 D，但不会把原 AI 上限、建议和检查状态改成 D/通过。

同一 revision 的并发保存仅一个成功，另一个返回 409。补充材料也要符合员工/周期；`needs_review` 仅可用于人工复核，必须在 `acknowledged_quality_issues` 列出 `snapshot_id:issue_code`，原始 AI 输入不会更新。最终请求带最新 revision 和 `limitation_disposition`，所有范围项必须有人工处理；风险项必须明确 `not_present` 或 `confirmed`，仍待调查不能定稿。定稿后不可再改，AI gate 原样保留。

缺失分值和差距是 null，不填 0；AI、岗位推荐和人工结果分别返回。非评分组的 AI grade 始终 null。本地定稿仍标记 `synthetic=true`、`final_employee_decision=false`，不是正式员工决策。

v9 的详情响应增加冻结 `job_key`、每项 `criteria` 与 `policy`，供逐档标准查看；来源是创建时的 `source_json`，不随当前员工岗位或新注册目录变化。`framework_scope` 只提供当前 MEDIC 框架的 17 个代码用于标注覆盖率，`rows` 仍严格限定本次评估范围；不补入范围外推荐值、标准或评分。详情接口继续执行员工范围授权。

## v10 PDF 原页预览

材料摘要增加 `preview.supported` 与 `preview.page_count`。当前仅 PDF 支持原页；Office 返回 415，不以重排文本冒充原件。预览返回 `image/png` 与 `X-Preview-Info` JSON 头，记录原件 hash、页码/页数、实际像素、原件旋转角、预览版本、图片 hash 和红框状态。响应保持 `Cache-Control: no-store`，无认证的图片直链不可访问。页面通过带认证请求取得 blob URL，退出、切换员工或关闭证据时释放。

`page` 范围 1–500，`width` 范围 320–1600，长页进一步限制为 400 万像素、8192 像素高。`evidence_id` 必须来自同一快照且页码一致；客户端不能传任意坐标。渲染子进程再次匹配冻结原文 hash、文本块编号与坐标，再将未旋转页坐标变换到旋转后的页图画红框。无法精确匹配时 `highlight=unavailable`，仅返回无框页图，不猜测位置。标注覆盖完整文本块，不声称精确圈出句子中的每个词。

每份材料库跨 API 进程最多 2 个渲染任务、每账号最多 1 个；忙时返回 429。同步渲染线程在请求取消后仍持有占位，直到工作完成。子进程默认 30 秒 / 768 MiB，最多 16 MiB PNG 输出；Windows 实际解释器显式加入受限 Job，父端关闭 Job 会终止整棵进程树。渲染前后都检查原文件与授权，期间撤销范围不能释放图片。页图与临时文件不作为新证据存储，不修改原件、提取结果或 AI 报告。生产 OS 沙箱、网络隔离、持续负载与留存策略仍需 P5 验证。

## 验证边界

`test_api.py` 同一组用例在 SQLite 和真实 PostgreSQL 执行，覆盖范围越权、撤销、上传质量、创建中断恢复、引用、并发修订、人工风险处理和原 AI 不可覆盖。报告 fixture 明确仅测试 API 流程，不代替真实模型执行证明。三类角色网页已完成合成流程核验；生产 SSO、组织同步、生产配额签收、存储服务、备份留存和签收仍待完成。

## v8 上传准入

同一材料库默认最多 2 个上传/解析任务、同一账号最多 1 个，跨 API 进程使用 OS 文件锁共享。占位从接收请求体持续到解析及发布结束；客户端取消不会提前释放已经交给线程的解析任务。锁文件不能删除。进程死亡由 OS 释放锁，未结算的完整文件预算仍计入配额。

最近 24 小时默认每账号 100 次 / 250 MiB、每租户 1000 次 / 1 GiB。准入先预留完整 25 MiB，正常退出按实际接收字节结算；不足一个完整文件预留量时拒绝。失败、空文件和中断仍计入尝试次数，繁忙/越权/预先判定超长请求不会消费次数。用量按稳定租户/账号标识记录，换 token、换员工、重启不能重置。记录写入 vault/snapshots.sqlite3，随现有材料库备份；这是入口配额，不是磁盘容量、留存或删除策略。默认是工程限值，尚无客户签收。

## v12 Office 转换预览

Windows 上可为 DOCX、PPTX、XLSX 配置固定 LibreOffice 引擎。采用与 PDF 相同的授权预览入口；Office 响应增加 `derived=true` 和 `conversion`（引擎版本、引擎清单 hash、系统字体 hash、派生 PDF hash）。原件、解析快照、质量状态和 AI 输入均保持不变。页面明确标为转换页：分页、字体和公式显示可能与 Microsoft Office 不同；可见图片或图表不代表已做 OCR 或图表理解。

当前固定 LibreOffice 26.8.1.1，官方 MSI hash、有效签名及完整制品核验见 [引擎来源记录](../../evals/results/p3-office-engine-verification.json)。本地制品为 `medic/artifacts/office-converter/26.8.1-pinned.zip`，包含引擎及相邻的 `26.8.1.manifest.json`，保留许可证。部署时复制该固定制品并先校验 archive SHA-256；设置 `MEDIC_OFFICE_ENGINE_ROOT` 指向解包后的 `26.8.1` 目录，默认使用上述本地路径。不要用其它版本的系统 LibreOffice 替换，或仅更新清单来绕过固定 hash。每次新转换校验全部引擎文件；缓存命中仍验证原件、缓存元数据和派生 PDF。未配置引擎时保留结构原文和原件下载。

转换使用临时用户配置和私有 UNO named pipe，不开放 TCP 控制端口。加载时 `MacroExecutionMode=0`（NEVER_EXECUTE）、`UpdateDocMode=0`（NO_UPDATE），交互请求只允许中止；启动后实际读取并确认禁用宏及自动更新。OOXML 预检查拒绝宏、嵌入对象、外部资源、未知动态域和不支持的公式，普通 HTTP/HTTPS/mailto 超链接仅作为静态文字。网络/外部工作簿函数不在公式许可范围，命名公式也检查。此准入策略不等于 OS/网络沙箱；更广格式、特殊域及公式支持仍需扩展。

单次转换上限 30 秒、768 MiB Job 总内存及每进程内存、8 个进程、64 MiB 派生 PDF、500 页。保留材料库全局 2 / 账号 1 的跨进程预览占位，取消请求不会提前释放运行中的转换。超时关闭整个 Job；渲染前后重新核验授权。PDF 像素/输出上限沿用 v10。

派生文件缓存位于 `vault/office-previews/<runtime>/<manifest>/<scope>/<snapshot>/`，由跨进程锁串行生成并原子发布；绑定原件、快照、引擎、字体和当前执行身份。不同版本不覆盖旧缓存。缓存只用于展示，不是证据存储；现有应用/材料备份不包含它，恢复后按当前固定引擎重新生成，并产生新的转换 hash。原件与提取快照仍由原备份流程保护。生产缓存容量、留存/删除及系统隔离未验收。

引用只在原始提取块中唯一、转换 PDF 中也有唯一完整词边界匹配时显示红框，并可自动跳页。短于 8 个非空白字符、长于 1024 字符、重复或无法匹配的引用不画框；不凭幻灯片编号、Excel 行号或近似相似度猜测位置。空白折行允许归一化，除此之外不改写引用文本。红框是转换页上的文本匹配范围，不能当作原生 Office 对象坐标。

合成测试文件可使用固定引擎的 `program/python.exe` 运行 `medic/evals/create_office_fixtures.py --engine-root <绝对引擎目录> --output <新的合成输出目录>` 生成；命令环境设置 `PYTHONDONTWRITEBYTECODE=1`，避免修改固定引擎。将输出路径传给 `MEDIC_TEST_OFFICE_FIXTURES` 后执行 `python -m unittest medic.tests.test_office_preview -v`。没有固定引擎时真实转换用例会明确跳过，不能据此宣称转换验收通过。

接收阶段总计限时 60 秒；解析仍由独立子进程限制控制。并发或配额不足返回 429，接收超时返回 408。通过 `--upload-policy <本地 JSON>` 指定 UploadPolicy 字段；首次使用绑定到材料库，后续实例必须完全一致，不允许不同进程偷偷采用不同上限。修改已绑定策略需要停机迁移，目前没有网页配置入口。

## v13 扫描件辅助识别

PDF 上传自动尝试本地简体中文/英文 OCR，仅处理无原生文字的栅格扫描页，每份最多 5 页。识别运行于现有受限解析进程，30 秒总时限、768 MiB 内存、400 万像素/页。OCR 模型固定哈希，缺失或损坏会保留缺口，服务不联网补齐。配置与数据归档见 [解析说明](../../ingestion/README.md#v3-本地扫描页-ocr)。

材料仍处于 `needs_review` 或 `empty`，`POST /assessments` 继续返回 `material_requires_review`（409）；没有新增签收或绕过门禁的接口。块接口保留 `ocr_text_requires_review` 标记及独立 `ocr_block` 定位。对其调用预览返回 `highlight=ocr_page_only`，仅展示正确原页，不生成原生文本红框。识别的引擎、语言数据哈希与无置信分声明保存在不可变快照中。旧解析版本及评估保持原值。
