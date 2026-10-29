# P1-04 第二小步：两套分块、父子上下文与表格

日期：2026-10-26。本小步完成，P1-04 仍进行中。本轮不进入 P1-05；练习附范例和参考答案。实现提交：`c6cf0f3`。

## 目标与必要原理

依赖首步固定的页/字段文本和引用定位器。本步将文本变成可检索的叶块，同时保留原文父块、标题路径、车型范围和版本。后续检索负责找块，调查负责核对其完整上下文，两者不能用一段顺口的摘录替代。

关键原理：小块容易命中，但也容易丢失前提；父块保存完整段落/章节，显式上下文保留跨段条件。表格的含义由列名、行标签、数值、脚注共同决定，只存数字或拆掉表头会改变理解。固定长度对照是保留的实验分支，不能先宣称其效果更差。

本步所有处理均使用固定本地资料，不调用模型，不改变原文，也没有把文档内容当作实现指令。

## A：先把配置和范围写成可校验契约

`parsing/chunk_contracts.py` 新增 ChunkConfig、TextRange 和版本化结构配置。固定窗要求 `0 <= overlap < characters`，保证窗口向前推进；章节/子块范围有非空边界和文本哈希，上下文引用必须存在且无环。

PDF 的结构依据绑定确切来源版本和解析指纹。当前已审查 PE10006 原页，配置四个章节：调查信息、产品范围、统计表、行动/总结。未知版式会拒绝结构化构建，不猜标题、范围或表格含义。

两套配置保留在 examples/chunk-structure.json 和 examples/chunk-fixed.json。固定版本使用 700 字符窗口、100 字符重叠；两者都对不超过 4000 字符的短投诉保持整条。这些数值是教学配置，没有测出“最优”。

## B：子块、父块与必需上下文

`parsing/chunks.py` 构建 document 根、section 或 page/field 父块和可检索子块。结构化表格整体保留；正文按审查过的段落切分；JSON 长叙述按空行分段，不截断单段。

```python
pending.extend(node["required_context_ids"])
if identifier == chunk_id and node["parent_id"] is not None:
    pending.append(node["parent_id"])
```

展开包含完整直接父块，以及显式要求的范围上下文；继续递归上下文依赖，但不默认把长文档全部放进提示词。根节点仍可回查。固定版本有同样的原始数据、父块和范围访问能力，后续对照才公平。

一个跨页段落可以保存多个原文范围；展示时用两个换行拼接，引用时为每个原文范围分别生成 Citation。它们保留各自真实页码和偏移，不使用拼接后的字符位置伪装成一个连续原文区间。真实材料只有一页，跨页机制已用合成多页 PDF 验证。

## C：整表处理与真实跨段反例

已读完整原页并核对统计表。表头为 ODI / Manufacturer / Total，6 行数据，共 18 个数值单元格；脚注 `*Description of Other: Warranty claims` 一并保留。程序按已审查模板验证列名、行标签、行数、数字和脚注，再记录每个数值的源位置。这是特定版式的严格处理，不是通用表格识别器。

真实跨段例子：前文 Products 写着 `Model Year 2010 Toyota Prius`，后文却使用 `The subject vehicles`。仅看后文，容易把材料误写成关于所有混动车型的结论。结构化正文附带前文范围章节；固定窗口也可展开完整父页和相同范围信息。

参考答案：subject vehicles 在本材料中应回指前文的产品范围，不能从这个片段推出“本调查已确认所有混合动力车型都有该问题”。这是依据真实材料构造的错误说明，不是运行某模型得到的错误输出，也未测量准确率、Recall@k 或 nDCG。

两套方案在该页都得到 6 个可检索子块，但边界和父块结构不同；相同块数不代表内容或检索效果相同。12 个叶块引用全部回放成功；18 个表格数值逐一与源字符位置对应。103 条真实短投诉在两套方案中都保留整条，长度超过 4000 的真实样例本批次为 0，长叙述机制只通过合成测试验证。

## 范例与参考答案

本机已有首步的解析工件，可直接构建两种版本：

```powershell
$parseDigest = '0fe534557720cdae5ad25ae52cae06d4df58d9bdb406765c92bec580c1278a42'
$structured = uv run --locked --offline python -m parsing chunk $parseDigest --strategy structure | ConvertFrom-Json
$fixed = uv run --locked --offline python -m parsing chunk $parseDigest --strategy fixed | ConvertFrom-Json
```

参考答案：两者 status=chunked、searchable_chunks=6。ChunkSet 保存在返回的工件哈希对应文件中；重复构建哈希不变。不同规则/配置的结果可以并存，不能修改旧工件。

生成一份新检查页：

```powershell
uv run --locked --offline python -m parsing review-chunks $structured.chunk_set_artifact.sha256 --compare $fixed.chunk_set_artifact.sha256 --output .local/my-chunk-review.html
```

参考答案：review_saved。用浏览器打开该文件，展开正文的“查看完整父块与必需上下文”，应能看到前文车型范围；统计表应同时展示三列、6 行与脚注。文件不能覆盖，重复练习可换新文件名。

已生成并目视检查的本机页面是 `.local/P1-04-chunks/review-final.html`；截图是 `.local/P1-04-chunks/table-comparison.png`。页面只依赖本地 HTML/CSS，可直接打开；验证用临时回环服务和标签页已关闭。最终页面与检查预览仅生成的块 ID 不同，模板、正文、表格和展开内容一致，已做归一化内容比较。

完整真实资料回放：

```powershell
uv run --locked --offline python -m scripts.verify_chunks
```

参考结果：structure=6、fixed=6、table_cells=18、quote_replays=12，两套各保留 103 条完整短投诉。其他机器需先保存相同原文件并建立解析工件；若版本变化，重新审查和升级结构规则，不能改写旧版本假装复现。

当前练习附答案：

| 动手场景 | 参考答案 |
| --- | --- |
| 只取后文 subject vehicles 一段，写成所有混动车型 | 范围被扩大；需回查产品范围，拒绝该更强结论 |
| 只取表中 1,427，省略列名和行标签 | 含义不充分；需同时核对 Complaints 行、Total 列及完整表格 |
| 固定窗口在表格中间截断 | 原样保留边界作为对照；解释时仍可展开完整父页与范围 |
| 相同 URL 新内容仍使用旧结构 | 拒绝，来源版本不匹配；旧引用不变 |
| 表头变化、缺脚注、数值列含未知符号 | 模板检查失败，不猜列或补值 |
| 章节重叠、子块越界、上下文有环 | 拒绝构建，避免重复或错误范围 |
| 解析只成功一部分 | 不将其构建成完整可信分块 |
| 跨页段落由两段来源拼接 | 两个来源范围、两份引用，分别回放到实际页码 |

```powershell
uv run --locked --offline python -m pytest -q tests/test_chunks.py --basetemp .local/practice-chunks
```

参考结果：24 项合成测试通过，覆盖父子上下文、短投诉整体保留、窗口进度/覆盖、表格形状、跨页引用、版本变更、篡改及 HTML 转义等。

## 验证、问题与剩余工作

累计 201 项无数据库测试、原有两组各 7 类真实回环 HTTP 检查通过，服务均关闭；Ruff、选定 35 个源码文件 mypy 与迁移 SQL 生成通过。真实 JSON/PDF 分块对账和浏览器检查单独记录，非模型质量实验。无新增依赖、数据库迁移、外部来源请求或真实模型调用；本轮未重跑既有 38 项 PostgreSQL 测试。

初次扩展 CLI 时，chunk 分支执行后还落入旧引用分支，出现先输出 chunked、后文件读取失败的问题；已改为互斥分支，并加入回归测试确保只返回一个成功 JSON。类型检查还发现将列表展开传给检查页函数的参数不明确，改为显式两个集合参数。后续格式检查修正了上一小步文档代码示例的切片空格。所有检查最终通过，没有把失败写成成功零结果。

公开记录：`docs/verification/2026-10-26-chunks-real.json`、`docs/verification/2026-10-26-chunks-offline.json`。规则说明见 `docs/data/chunking-rules-v0.1.md`。结构、参考结论和页面由实现代理核对，不冒称用户独立标注或独立人工金标准。

P1-04 仍未完成：下一步汇总机器可读失败清单、补充否定/条件与引用整体验收，并核查当前保守结构覆盖边界。OCR 尚未执行，跨页表格仍需独立模板；未知资料继续明确拒绝而非猜测。模块完成后等待理解确认，再进入 P1-05，完整目标保留。
