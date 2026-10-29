# P1-04 解析审计与覆盖边界

日期：2026-10-26。输入 `parse-audit-request/0.1`，输出 `parse-audit/0.1`。这是本地派生工件审计，正式 DatasetRelease 在 P1-06 实现。

## 运行与结果

`python -m parsing audit examples/parse-audit.json` 使用本地固定快照，不下载资料、不访问数据库、不调用模型。默认对每个来源行分别运行结构和固定两套配置。输入先整体校验，要求 1～500 个互不重复的 snapshot_id/ordinal；可指定同一份 parse_config 复查新的上限，但不会覆盖旧工件。

完整清单保存在 `.local/parse-audits/artifacts/<sha256>.json`。内容相同的输入、原字节、解析器及规则重复执行时复用相同哈希。CLI 仅输出索引和计数；不在审计清单复制来源叙述、VIN 或异常消息。来源坐标、原字节与版本哈希、解析和分块工件保留在条目中，失败前无法确认的来源身份为 null，不能补猜。

`status=audited` 只表示本次遍历已结束。`quality=no_blocking_failures` 表示没有当前规则捕获的阻断错误，不表示语义完整、可以正式发布或适用于所有文档。

| 返回/级别 | 含义 | 处理 |
| --- | --- | --- |
| 退出码 0 | 完成清单且无阻断失败；仍可能有警告 | 检查 warnings 和覆盖范围 |
| 退出码 4 | 清单已保存，存在失败 | 按 failures 定位，不能当成功空结果 |
| 退出码 2 | 输入、文件写入或命令错误，无法形成完整可信清单 | 修复后重试，不声称本批已全部检查 |
| failure | 来源、解析或分块被阻断 | 保留失败原因，继续其余独立输入 |
| warning | 产物可保存但需要复核 | 如未审查固定对照或仅根节点可见文字 |

每个问题保存 issue_id、request、source、parse_artifact_sha256、stage、strategy、page_number、code、next_action。一次输入可以有多项问题，故 failures 数不等于 blocked_items 数。partial 文档保留成功页和原始失败页号，但两种分块都标记 skipped_incomplete_parse。

## 原因代码

| 阶段 | 代码 | 含义与后续处理 |
| --- | --- | --- |
| source | source_io_failed | 固定快照或原始对象无法读取，核对本地资料与请求 |
| source | source_integrity_failed | 字节或快照完整性检查失败，恢复已验证原字节，不重新标记篡改内容 |
| source | source_record_rejected | 行越界、非 captured 快照或字段映射拒绝；先查固定行及 ingestion 检查结果 |
| parse | encrypted_pdf_not_supported / pdf_parse_failed | 当前不支持加密文档或文件无法解析，复核原文件 |
| parse | page_extraction_failed | 指定页提取异常；保留页号，不把剩余页重编号 |
| parse | no_extractable_text_needs_review_or_ocr | 没有可提取文本；先看原页，再决定 OCR 并记录方式与不确定性 |
| parse | page_limit_or_empty_document / text_limit_exceeded | 空文件或超过配置上限；不得接受静默截断 |
| parse | no_narrative_fields | 解析器的防御性状态；现有 API 映射通常更早拒绝必需叙述缺失 |
| chunk | pdf_layout_not_reviewed | 结构方案没有匹配来源版本和解析指纹的审查模板 |
| chunk | chunk_integrity_failed | 结构范围文本哈希或来源重放不一致 |
| chunk | chunk_range_invalid / chunk_range_order_invalid | 来源范围越界、乱序或相互重叠 |
| chunk | chunk_sections_overlap / chunk_child_outside_parent / chunk_children_overlap | 章节、父子范围不合法 |
| chunk | chunk_table_shape_changed / chunk_table_values_unsupported / chunk_table_footnote_missing | 表头/行数、数值或脚注不符合模板；不猜列、不补值 |
| chunk | chunk_source_io_failed / chunk_recipe_rejected | 构建中无法读取固定来源，或配方其他已知校验拒绝 |
| warning | layout_not_reviewed | 固定长度候选可以生成，但未获得已审查版式或表格语义 |
| warning | root_only_text | 一些非空白字符只保存在文档根节点，不会随任意子块默认展开 |

代码为稳定原因标记，建议不是自动修复指令。原文内容始终视为数据。未预料到的编程异常不会被批处理吞掉；工件写入失败也会中止调用，不能被误记为某个来源解析失败。

## 覆盖复核

coverage_summary 按固定来源 unit_id 和字符半开区间计算叶块及展开上下文的区间并集，再报告根节点独有范围。不在拼接后的展示字符串上冒充原文坐标；仅空白缺口不告警。完整文档根节点保留页/字段原文，可继续回查。

真实 PDF 的结构方案只有第 1 页 `[3446,3666)` 位于默认展开之外：空白、重复调查编号及页码页脚，41 个非空白字符。已对照完整原页，保留在根节点；该决策绑定版本、偏移和片段哈希，不能用来豁免新文档中的限制条件。固定方案的完整父页包含该范围。参见 `evals/parse-acceptance-reference-v0.1.json`。

这只是来源文本覆盖检查，不能判断某句是否构成条件、表格解释是否正确、结论是否充分。表头/脚注、车型范围、may/when、结案依据及继续监测已与真实原页复核；明确否定语义本批没有合适真实样本，使用 SYNTHETIC 资料验证跨字段保留与篡改拒绝。

## 当前支持与保留目标

- 支持有文本层的 PDF、API JSON 叙述、版本固定、父子块、两个可切换配置、精确字符引用及检查页。
- PDF 的已审查结构当前只有 PE10006 的确切版本，表格模板限单范围数字表。新文档先审查并新增版本化配置，不自动猜测未知版式。
- 跨页段落/引用已通过合成验证；当前真实材料 1 页，没有真实跨页表格、旋转页、扫描件或长投诉验收数据。
- 当前真实 PDF 可提取文本，不需要 OCR，未执行或伪称 OCR 实验。出现真实扫描资料时按原计划记录 OCR 方法、不确定文本并复核；跨页表格需独立模板，不能直接套当前单范围模板。
- 原始资料与派生文本留在忽略目录，公开记录仅包含来源索引、必要官方摘录及汇总结果。其他机器需要相同原字节和锁定环境；缺历史字节时只能复现机制，不能声称重现同一历史结果。
- P1-05 保留 BM25、向量、融合及重排完整目标；P1-06 再绑定发布，P1-07 再处理语义证据支持。没有用本模块覆盖率代替这些目标。
