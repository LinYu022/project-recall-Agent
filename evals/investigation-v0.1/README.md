# 固定调查基线的标签和审查协议 v0.1

本目录 8 题全部使用已经查看过的开发资料；不是未见测试集。标签由实现代理依据固定原文、既有计数/关系参考及 PDF 完整页建立，独立人工尚未审查。`cases.json` 固定任务输入、来源、范围和工具预算；每题 JSON 固定证据槽、OR/AND 组合、禁止推断和可接受处置。在真实生成之前运行 `scripts.prepare_investigation_cases` 建立这些文件。

评测输入仅含题目、任务范围、允许来源和工具返回，不包含本目录 reference_claims、evidence_sets、forbidden_inferences、得分或审查记录。当前开发题可以用于实现调试，但不得把同事件改写移入后续未见测试集。

## 审查顺序

1. 从保存的报告导出盲审包，将 `.json` 或 `.html` 交给审查者；不交 `.private.json`、运行目录、模型名或已有分数。无真实人参与时不填写 human。
2. 审查者阅读题目范围、全部原子结论、引用原文及必要上下文，对每条记录 supported/partial/overstated/not_evaluable、scope_correct、counterevidence_handled 和理由。`reference_claim_ids` 表示它实际覆盖的参考结论；没有覆盖可留空。
3. 记录 disposition_correct、complete、争议状态。澄清、限定范围无记录、时间不支持和证据不足分别判断；模型或引用故障是执行失败，不能标成“成功空结果”。
4. 导入盲审表后由私有映射恢复报告哈希，再运行评分。报告或标签变更会使旧审查不匹配，必须保留旧审查与旧结果。

## 新证据与争议的预定规则

- 匹配不到现有槽的引用进入 novel_evidence_pending，不能仅因未列入标签就判语义错误，也不能自动计为有效证据。
- 审查者在不知道方法的前提下检查出处、范围、条件和语义等价性，以证据别名提交针对参考结论的 accepted/rejected 和理由。将 `NovelEvidenceDecision` 的证据 ID 集合视为一个必须联合成立的组合。
- 有异议时标 dispute_status=pending，语义总分保持 null，另请第二位独立人员复核；没有第二位参与时保持待决。实际复核后才登记 adjudicated，不由程序合成裁定。
- 若需要修订公共标签，新增 v0.2 目录，登记提出理由、来源 locator、审查身份、时间和旧/新哈希；对所有方法的全部受影响题统一用新版本重评分。不能只修有利于某一种方法的结果，不覆盖旧版本。
- 实现代理可给带署名的临时审查供学习参考；它不构成独立盲评，不用于泛化性能结论。

## 指标

机器指标：运行完成、原文/结构化出处重放、Claim 机械约束、处置是否落在参考集合、引用槽覆盖。槽覆盖不是结论召回率，引用一整页可能覆盖多个槽但不代表结论真的表述了这些事实。

语义指标：supported_claim_fraction、grounded_claim_fraction、required_claim_recall、范围正确比例、反证处理比例和 disposition_correct。grounded 要同时满足审查支持及一个完整的预定/经盲审接受的证据组合。required_claim_recall 要求审查者确认结论实际覆盖参考结论；不使用文本相似度自动判定。空结论分母按指标记 null，不强行设为 0 或 100%。未完整审查、待决争议或 not_evaluable 时不产出语义总分。

## 最小审查格式范例（仅合成示例）

```json
{
  "blind_id": "从公开盲审包复制",
  "reviewer_kind": "synthetic",
  "reviewer_id": "example-only",
  "claims": [
    {
      "claim_id": "C001",
      "verdict": "overstated",
      "reference_claim_ids": [],
      "scope_correct": false,
      "counterevidence_handled": false,
      "reason": "引用真实，但把2010 Prius的有限措施泛化成所有混动车安全。"
    }
  ],
  "disposition_correct": false,
  "complete": true,
  "novel_evidence_decisions": [],
  "dispute_status": "none",
  "notes": "格式范例，不是真实人工评分"
}
```

没有 claims 的程序护栏案例按 `claims: []` 审查，不能虚构 C001。新增证据决定用 `evidence_aliases: ["E001"]`，导入程序转换为本次报告的真实证据 ID。
