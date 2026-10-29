# P1-04 第三小步：失败清单、条件与模块验收

日期：2026-10-26。P1-04 实现与本地验收完成，等待用户确认理解后进入 P1-05。本轮只完成 P1-04；练习直接提供范例和参考答案。实现提交：`bffae21`。

## 目标、原理与依赖

依赖 P1-02 的固定原字节/来源版本、P1-03 的来源范围，以及 P1-04 前两步的解析、引用和分块。本步补齐计划中的解析失败清单，核对否定/限制条件，并明确当前覆盖边界。后续检索应读取可追溯的工件；失败文档不能悄悄消失在索引之外，或者伪装成成功的零条结果。

需要分清四件事：批处理执行结束、每份来源解析成功、引用准确回放、结论语义受到支持。它们不是同一状态。清单的 audited 仅表示已遍历本次请求；没有阻断失败也不表示语义充分或已正式发布。

本步不使用模型识别否定词，也没有构建通用语义裁判。我们先证明来源文字/上下文没有丢失、篡改能被拒绝，再把结论支持判断留给 P1-07。检索比较保留在 P1-05，正式发布绑定保留在 P1-06。

## A：批量审计契约与失败分层

新增 `parsing/audit.py`。AuditManifest 先检查请求列表非空、最多 500 项、snapshot_id/ordinal 不重复；输入通过后才开始生成工件。每项来源沿用原解析器，再分别构建两套配置，不复制一套简化的解析逻辑。

核心分支是：

```python
if parsed["status"] != "parsed":
    for problem in parsed["issues"]:
        issue(item, problem["code"], "parse", page=problem["page_number"])
    # 两套分块都跳过，保留成功页及失败页号供复核。
    continue
```

清单按 source / parse / chunk / coverage 分层，记录失败或警告代码、固定来源、页号、解析工件和下一步建议。源码的 ChunkBuildError 保留机器可读 code，仍是 ValueError 的子类；现有调用者的拒绝行为不变，不靠英文异常消息猜原因。

完整失败清单和成功条目进入 `.local/parse-audits/artifacts/`，以内容哈希保存。审计不复制原始叙述、VIN 或异常消息；可以通过工件/位置回到原始资料。某份来源失败后继续其他独立输入；磁盘写入失败和未预料到的编程异常不吞掉，否则会把工程故障错记成来源质量问题。

CLI 新增 `audit`，支持 --audit-root；main 也接受可选 argv，使 CLI 测试不必改全局参数。退出码 0 表示没有阻断失败，4 表示清单已保存但有失败，2 表示调用/写入等错误未能形成完整清单。0 仍可伴随需复核的 warnings。

## B：看得见的覆盖边界

coverage_summary 将叶块及其展开上下文的原文区间合并，报告只位于完整文档根节点的非空白范围。它按 unit_id 与字符偏移计算，不使用拼接展示文本的偏移冒充原文位置。

真实 PDF 结构方案只有第 1 页 `[3446,3666)` 未默认展开，共 41 个非空白字符。对照完整原页后确认仅包含重复调查编号和页码页脚；完整原文、根节点仍保留它们。固定方案的完整父页也包含该范围。该决策单独存入版本化参考文件，绑定准确位置和片段哈希；换一份文档不能继承这次页脚豁免。

如果遗漏的是适用条件或脚注，正确处理是重新审查并生成新版结构规则。不能把所有未覆盖范围都叫噪声，也不能只用“根节点有全文”宣称默认上下文已经完整。

## C：真实条件与合成否定分别验收

沿用已保存原 PDF，重新查看完整原页渲染。`evals/parse-acceptance-reference-v0.1.json` 保存四项来源复核结果：

| 原文信息 | 参考答案 / 禁止扩大 |
| --- | --- |
| Model Year 2010 Toyota Prius | 范围为对应年款车型，不能扩大为所有混合动力车 |
| may occur during braking events requiring ABS operation，举粗糙或低摩擦路面为例 | 保留“可能”和 ABS 条件，不改成任何制动必然发生；也不把例子说成穷尽条件 |
| Based on Toyota's action the investigation is closed | 保留结案依据，不能由结案推断不存在缺陷 |
| ODI will continue to monitor…take further action as future circumstances dictate | 保留继续监测和后续行动说明，不能删成机构不再关注 |

`scripts.verify_parse_acceptance` 先批量审计 1 份 PDF、2 条召回车型记录、103 条投诉，再检查两套方案的上述四项准确摘录、上下文和引用。共 106 条来源、212 份分块工件、8 项条件回放；阻断失败 0、根节点页脚警告 1。该警告已与固定参考决策比较，不是看到 warning 就自动通过。

真实文件没有“未确认缺陷”这样的明确否定结论，所以没有伪造真实否定实验。19 项新增合成测试中，跨字段范例明确标记 SYNTHETIC：Summary 只说有症状报告，Consequence 写 do not establish a confirmed defect，Remedy 写 only package A / excludes package B。两套方案都能展开完整字段，引用逐一回放。删除 do not 中的 not，即使重新计算摘录哈希，也无法改变固定原文，因此验证拒绝。

合成测试还验证相同 URL 新内容不会替换旧分块和含否定词的旧引用。引用正确但结论过强的错误则属于语义支持问题，不能声称被本模块的哈希检查全部检出。所有参考答案由实现代理核对，未冒称用户独立标注或独立人工金标准。

## 范例、练习与参考答案

成功场景：

```powershell
$result = uv run --locked --offline python -m parsing audit examples/parse-audit.json | ConvertFrom-Json
$result.counts
$digest = $result.audit_artifact.sha256
Get-Content ".local/parse-audits/artifacts/$digest.json" -Encoding utf8 | ConvertFrom-Json
```

参考答案：audited、quality=no_blocking_failures、requests=4、parsed=4、chunk_sets=8、failures=0、warnings=1，退出码 0。重复运行返回相同审计哈希。展开 items 能找到每份解析与两份分块引用；warnings 的 root_only_text 对应已说明页脚。

受控失败场景（假定全零 ID 在本地不存在）：

```powershell
uv run --locked --offline python -m parsing audit examples/parse-audit-missing-snapshot.json
$LASTEXITCODE
```

参考答案：仍生成 audited 清单，但 quality=failures_present；requests=1、parsed=0、chunk_sets=0、blocked_items=1、failures=1，退出码 4。完整清单的 failure 位于 source 阶段、code=source_io_failed，source 与 parse_artifact_sha256 为 null。不能把 0 个工件说成“该来源没有任何记录”。

已实际运行这个缺本地输入的受控反例，公开记录在 `docs/verification/2026-10-26-parse-audit-failure-example.json`。它不代表官方真实 PDF 出现解析失败；当前真实资料没有阻断失败。

完整本机来源验收：

```powershell
uv run --locked --offline python -m scripts.verify_parse_acceptance
```

参考答案：status=passed，requests=106、parsed=106、chunk_sets=212、failures=0、warnings=1，condition_checks=8。其他机器需保留相同字节并用 --pdf-snapshot / --recall-snapshot / --complaints-snapshot 指定本机观察。来源版本变化时应新增结构和参考配置，使用 --reference 指定新文件，不能改旧参考来冒充同一历史结果。

否定/失败机制范例可运行：

```powershell
uv run --locked --offline python -m pytest -q tests/test_parse_audit.py --basetemp .local/practice-parse-audit
```

参考答案：19 项合成测试通过。无文本返回 needs_review_or_ocr；部分页返回 partial 并保留页号；两者不构建完整可信分块。未知版式拒绝结构方案，但固定对照保存 layout_not_reviewed。表头不符返回 chunk_table_shape_changed。篡改原字节返回 source_integrity_failed，缺必需投诉叙述在映射阶段返回 source_record_rejected。磁盘/编程故障会中止，不会偷偷生成成功清单。

## 验证记录与修正

新增 19 项合成测试，累计 220 项无数据库测试通过；原模拟和新查询两组各 7 类真实本机回环 HTTP 检查通过，两个服务自动关闭。Ruff 格式/静态检查、选定 36 个源码文件 mypy、迁移 SQL 生成通过。无新增依赖、迁移、外部来源请求或真实模型调用。本轮未重跑既有 38 项 PostgreSQL 集成测试；没有将本地等价 CI 说成 GitHub Actions 已运行。

首次补充测试错误地预期“投诉 summary 缺失会进入解析失败”，实际现有字段映射已先拒绝它。按既有分层规则修正为 source/not_parsed，并补充断言，不放宽输入契约。代码整理时删除了一个未使用的测试导入；最终所有检查通过。

公开记录：

- `docs/verification/2026-10-26-parse-acceptance-real.json`：真实本地来源与固定条件回放。
- `docs/verification/2026-10-26-parse-acceptance-offline.json`：合成/假模型的完整回归及本机 HTTP。
- `docs/verification/2026-10-26-parse-audit-failure-example.json`：实际执行的缺本地快照受控失败。
- `docs/data/parsing-audit-v0.1.md`：状态、失败代码和支持边界。

## 模块产物与继续入口

P1-04 交付已齐：页/字段解析器、短投诉整条、审查结构/固定窗口两套配置、章节/段落/表格及父块、HTML 检查页、稳定引用定位器、批量失败清单和模块验收。前步已核对表头、6 行、18 个数值与脚注；本步保留相同解析/分块产物，新增失败和条件审计。

当前接受的覆盖边界是明确的：已审查 PDF 仅 PE10006 这一版本，数字表模板仅支持单范围；跨页段落和引用仅有合成验证，真实跨页表格/长投诉/扫描件尚无本批材料。当前不需 OCR，未执行 OCR。保留原计划按真实材料扩展审查配置、必要时记录 OCR 方法和不确定文本的要求；不把未知版式或 OCR 猜测变成可靠原文。

建议把“新增资料时复核版式、仅根节点内容及 OCR 需要”作为持续数据验收步骤，避免把一个样例通过误当作通用 PDF 能力。原计划副本不修改、不删除后续功能。当前资料仍属探索范围，已看事件不进入未见测试集，尚无正式发布或模型效果实验。

等待用户确认理解后，下一模块为 P1-05：BM25、向量、融合与重排。先学习过滤与排序、索引输入和可解释的稀疏检索，再逐步实现全部检索比较目标。
