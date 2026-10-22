# 材料导入与证据快照

`medic-extraction-v3` 解析 PDF、DOCX、PPTX、XLSX，保存原始文件的内容哈希、解析器与应用构建版本、提取文本、独立检索文本、结构定位及质量缺口。它不执行宏、外部链接、公式或文档中的指令。客户材料不会送入模型；生产评估入口仍由 runtime 禁用。

```powershell
python -m pip install -r medic/requirements-ingestion.txt
python -m medic.ingestion <local-file> --tenant <tenant-ref> --employee <employee-ref> --period <period> --root medic/var/materials
```

CLI 只输出快照 ID、hash、数量及固定问题代码，完整内容保存在指定的私有目录。这里的 tenant/employee 是未来应用授权层传入的可信范围参数，当前 CLI 本身不提供登录或组织关系验证，不能作为生产访问控制接口。

| 格式 | 定位和保留内容 | 质量边界 |
| --- | --- | --- |
| PDF | 页码、文本块、MuPDF 未旋转页坐标、原生提取文本 | 图片、无原生文字页、矢量图、注释及表单保留问题项；纯扫描页可生成待核对 OCR 文字，图表含义仍未解释。 |
| DOCX | XML 部件、段落、表格行列结构路径；页眉页脚、脚注尾注及批注文本 | 不声称 Word 渲染页码；修订、隐藏文本、图片/嵌入对象及动态内容需复核。 |
| PPTX | 实际演示顺序、shape ID、表格行列、备注层 | 图片、图表、嵌入对象单独标出；备注与幻灯片正文定位分开。 |
| XLSX | 实际 sheet ID/名称、cell、隐藏表/行/列、合并范围和样式索引 | 分开保留公式正文、原始缓存值与缓存状态。没有重新计算；复杂公式、错误单元格和未缓存公式不能自动进入评估。原始数值不伪装成经过 Excel 渲染的日期/百分比。 |

v2 对 PDF 逐页比较 MuPDF 与 pypdf 的非空白字符计数。字符数量/种类不一致或第二解析器失败会产生阻断性问题，保留原生文本及坐标，不自行拼补。该检查能发现一部分漏字或裁剪差异，不证明阅读顺序、图表、OCR 或语义已正确；仍须按质量提示复核。

`quote` 保持提取时的空白、制表符和换行；规范化只作用于 `search_text`。PDF 不重复保存整页文本作为另一份证据。每次上传或重新解析生成新的 snapshot ID，即使原始字节相同；相同范围内的原文件 blob 复用，历史快照永不覆盖。快照和源文件在每次读取时核验 hash。重复字节或同一 `source_event_id` 会在 runtime 同源分组中合并，不能通过改文件名或事件标签制造独立佐证。

`ready` 表示当前文本提取没有阻断性缺口；`needs_review`、`empty`、`failed` 均不能转换成自动评估的 Material。解析状态不是员工能力判断。现阶段未提供缺口人工签收后继续评估的 API；不允许靠修改状态绕过。可信应用后续需把质量报告展示给上传者和复核者。

原文件 SHA-256 与旧 runtime 的文本集合 hash 分开：`source_sha256` 标识原文件，`Material.sha256` 保持为证据 quote 集合 hash，`snapshot_ref` 关联不可变提取快照。Span hash 绑定 snapshot ID、结构 locator 和 verbatim quote；重传后旧引用仍指向旧版本。

每文件独立解析进程，默认输入 25 MiB、ZIP 总展开 128 MiB、单成员 16 MiB、4,000 成员、20,000 文本块、8 Mi 字符、500 页、30 秒、768 MiB 内存。超限返回失败而不是悄悄裁剪。ZIP 全部成员计入预算，拒绝路径穿越、重复成员、加密成员和 XML 实体/DTD；不把 ZIP 解压到文件系统。Windows Job Object 同时限制单进程与整个作业的内存，最多两个进程以容纳 venv 启动器及解释器，关闭作业时一并终止；POSIX 使用资源限制。超时会终止实际子进程。

解析子进程只继承解释器正常加载依赖所需的环境变量，剔除模型凭据；进程与父服务的应用/依赖身份必须一致。该资源边界不等同于完整 OS 文件/网络沙箱，生产容器或服务账户隔离仍属于 P5。解析器安装目录和数据库写权限属于可信管理边界。

验证入口：`python -m unittest medic.tests.test_ingestion -v`。合成测试包含未缓存公式整行、部分缓存、隐藏表/宽表、结构定位、扫描/加密 PDF、压缩包与 XML 攻击输入、真实超时与 Windows 内存限制、跨员工读取、篡改及重传去重。实际参考材料审计仅写 hash/数量摘要，不把客户原文放入公开测试集。

## 原 PDF 页图与证据红框

`medic-pdf-preview-v1` 在独立受限进程中按需生成原 PDF 页图，供应用复核。它不重新抽取或发布 EvidenceSpan，也不把 `needs_review` / `empty` 改为 `ready`。扫描页可以显示；v3 的 OCR 文字单独标为待核对，图表和图片的解析缺口继续保留。Office 已提供转换页预览及唯一文本匹配，见 [API 说明](../apps/api/README.md)。

红框输入由服务端按冻结证据 ID 取得，子进程验证原文件 hash、文本块 quote hash 和 bbox。依据 [PyMuPDF 的旋转坐标约定](https://pymupdf.readthedocs.io/en/latest/page.html#Page.rotation_matrix)，将未旋转坐标映射到实际页图。红框只写入输出 PNG 像素，不修改 PDF；裁剪后再旋转的页面若直接插入 PDF 图形可能偏移，像素回归测试覆盖 0/90/180/270 度及裁剪组合。原始失败用例保留在 v10 私有验证日志，修复后逐像素定位检查通过。

默认上限沿用 25 MiB 输入、500 页、30 秒和 768 MiB 进程树内存，并额外限制 400 万输出像素、8192 像素高及 16 MiB PNG。大幅面页面自动降低显示分辨率；异常页尺寸或渲染失败不发布替代图。页码不存在、加密 PDF、原件变化、版本混用均失败。`test_preview.py` 验证旋转/裁剪、错误原文和错误坐标、像素预算、空白页不改质量状态、真实子进程、超时与跨实例并发；API 测试验证授权及取消边界。

## v3 本地扫描页 OCR

上传入口在独立解析子进程中启用 `medic-ocr-v1`。只对含栅格图像且没有非空原生文本的 PDF 页面识别；混合页、Office 内嵌图片、独立图片文件、纯矢量文字尚不支持。原有文字提取、跨引擎原生文本比较和质量问题不被替换。直接调用提取核心 `extract` 默认不启用 OCR，需明确传 `ocr=True`；应用与 CLI 的 `parse_file` 会启用。

使用已固定 PyMuPDF 1.28.2 / MuPDF 1.28.2 内置的 Tesseract 接口，语言为简体中文与英语。语言数据来自 Tesseract 官方 tessdata_fast 4.1.0，固定 commit `65727574dfcd264acbb0c3e07860e4e9e9b22185`，逐文件大小及 SHA-256 写在代码中。每页识别前将核验过的字节复制到私有临时目录，识别引擎只读取这些副本。默认目录为 `medic/artifacts/ocr/tessdata-fast-4.1.0`；管理员可用 `MEDIC_OCR_DATA_ROOT` 指定同内容目录。服务不会自动下载，也不会使用环境中未经核验的 Tesseract 模型。语言包及许可证归档核验见 [数据记录](../evals/results/p3-ocr-data-verification.json)。

每份文件最多尝试 5 页，每页最多 400 万像素，最高 200 DPI；自动降低到 100–200 DPI，若 100 DPI 仍超限则跳过并给出问题项。识别在页面私有副本上去除查看器旋转，保留裁剪框且不渲染批注，原文件和原页面不改写。整个文件仍受 30 秒、768 MiB、文本/块数上限限制，超时会终止进程树。语言包缺失、损坏、页面过大、超过页数或识别失败均保留明确缺口；文本预算超限则整个解析失败，不能无提示截断。

OCR 块使用独立 `pdf_ocr` 类型及 `ocr_block` 定位，保存识别原始文字、页码、坐标、引擎/语言数据 hash、DPI、识别版本。此接口不返回置信分，明确记录 `confidence=null`、`confidence_available=false`、`human_verified=false`，不编造可靠性分数。OCR 坐标只作为派生记录保存；页面目前仅跳转对应原页，不把识别框冒充可核验的原生文本红框。

有 OCR 文字仍为 `needs_review`，无文字为 `empty`。所有原有图片/无原生文字层问题继续保留。应用拒绝创建自动评估，材料库拒绝转换为自动评分 Material；网页勾选“我已核对”仅为本页提示，不是签收接口，不能解除门禁。OCR 结果仍可能漏字、错字或拆分阅读顺序，不代表图表理解或员工事实。后续人工签收与更广扫描质量评测尚待实现。

新上传生成 v3 快照；历史 v2 快照、原引用和评估输入保持原样。重新上传同一字节会生成新的解析快照，不覆盖旧结果。一次评估只接受同一解析版本的材料；合并使用旧材料时须重新上传并选择新快照，旧快照继续保留。冻结识别文字随现有材料快照备份；OCR 语言包作为发布依赖另行归档。

技术依据：[PyMuPDF OCR API](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_textpage_ocr)、[官方语言数据](https://github.com/tesseract-ocr/tessdata_fast/tree/65727574dfcd264acbb0c3e07860e4e9e9b22185)。
