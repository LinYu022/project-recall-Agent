# P1-16当前实现开发首批：真实结果

新计划`17a29b9475174de0b5277f6e8259fcb1353e25c57cce314662378380a224dde4`已执行结束，专用数据库停止并保留文件。源码与预检交付提交为`ab1b537`。本批属于两个已见开发事件簇上的默认固定流程小试；不是四方法比较，也不补改此前冻结测试。

## 实际执行与请求

用户批准上限10次，本计划执行上限3次，实际 **3次**：三个回答任务各1次，全部正常解析；失败/未知也占额的规则未变，没有自动重试。原P1-16的209次和P1-09的12次账本哈希保持不变，新批单独记账。[完整机器核查](../verification/2026-10-29-P1-16-development-live.json)绑定全部输入/响应、观察、报告和审查包。

| 任务 | 实际任务处理 | 请求 | 端到端秒 | API秒 |
| --- | --- | ---: | ---: | ---: |
| DEV2-PRIUS-RECALL | COMPLETED / answered，5条结论 | 1 | 244.02 | 2.818 |
| DEV2-PRIUS-JOINT | COMPLETED / answered，5条结论 | 1 | 299.52 | 3.296 |
| DEV2-FORD-PAIR | COMPLETED / answered，6条结论 | 1 | 253.96 | 3.701 |
| DEV2-MISSING-YEAR | WAITING_INPUT，要求model_year | 0 | 17.20 | 不适用 |
| DEV2-INDIVIDUAL | WAITING_INPUT，要求configurations/production_date | 0 | 16.55 | 不适用 |
| DEV2-COMPLAINTS | COMPLETED / insufficient_evidence | 0 | 73.95 | 不适用 |
| DEV2-WRONG-YEAR | COMPLETED / no_matching_records | 0 | 67.33 | 不适用 |
| DEV2-HISTORICAL | WAITING_INPUT，要求accept_frozen_time | 0 | 13.96 | 不适用 |

8格原状态为5 completed、3 guarded。前三份报告的程序证据校验verified、结论契约passed；它们不自动证明语义正确。其余零请求任务虽符合预期处理，独立语义判断仍不能由实现代理代填。

实际输入17006、输出1943、合计18949 tokens，缓存命中768/未命中16238，三次usage完整且加总一致。实际账户扣费未核验，货币费用为null。真实输入重新核对仅含本题允许来源，未发现参考标签/凭证字段；预检输入与真实输入分别保存，没有把预检改名为真实生成。

端到端耗时含普通发布/来源/数据库核验、检索、工具、证据校验与交付；API时间只计外部请求，不能把244秒说成模型响应时间。运行顺序固定，缓存/负载未随机化，不能从预检与真实运行的局部耗时差声称优化收益。

## 已收到的独立人工判断

用户实际审完BLIND-b0f073c1176a，对应DEV2-PRIUS-RECALL：[精确原判记录](P1-16-development-human-review-DEV2-PRIUS-RECALL.json)。C001～C005均支持、范围正确，各自直接引用为E002/E002/E003/E004/E001，无额外必需上下文；answered合适，核心任务完成，**全部题目必需条件满足**。条件、两项缺陷表现、碰撞风险与免费ABS补救均经其实际核对。客服/厂方代号等次要省略不影响本题，号码脱敏限制保留。

这是一份当前系统的新开发人工评价，与此前的历史P1-09开发正向审查、冻结测试负向审查分别记账。累计实际审查3份/17条结论，其中当前小试1份/5条。已读单方法方案的学习者知道方法，未声称严格方法盲法；判断来源仍是实际人类回复。

后续收到第二份实际审查BLIND-95cee9ebfdee：[联合调查原判](P1-16-development-human-review-DEV2-PRIUS-JOINT.json)。C001～C005均支持，answered合适、核心任务完成；C002遗漏certain（部分/特定）限定，范围仅大体正确。全部必需条件的原话为“基本全部满足（仅有一处轻微瑕疵）”，因此保留分类`mostly_satisfied_with_minor_caveat`及原话，二元字段为null；这表示带限定的已审判断，不是未审，也不偷换成全部通过或失败。

C004中E002仅为结案/监测原文的必需上下文，C005中E002同时直接支持主体范围并承担上下文角色。C005的Lexus来自C002已引的E005，审查者接受其作为上下文呼应；原C005的support_ids未被补入E005。递归程序核查4/4上下文链接齐全，不能用此机械覆盖率替代语义。审查者认为新增碰撞风险表述相对旧样例更完整，这一局部意见不推出模型或方法总体收益。

第二轮时已审2/8格、10条结论，6格待审；当时累计实际4份/22条结论（1冻结测试、1历史开发、2当前开发），不混算成功率。[第二轮汇总](../verification/2026-10-29-P1-16-development-review-round2.json)与[完整性核查](../verification/2026-10-29-P1-16-development-review-round2-integrity.json)保留原首轮汇总，并验证当轮0新增请求、原始运行/账本不变、错误转录被拒绝。

此前两份均由同一位学习者审查，也同属Prius事件簇；“独立”指实际人类评价与实现代理判断分开，不代表两位审查者或两个独立事件。

同日随后收到F-150六条及五项停止场景的实际审查，本批现已8/8任务获得评价。F-150全部支持、核心及全部条件满足；五项停止/有限回答均被认为合适、满足相应阶段要求。生成结论共16条、覆盖两个已见事件簇，完整条件分类为7满足/1基本满足带轻微瑕疵；原子指标仍未单独标全，不报严格全任务100%成功。人工有效阅读用时未测量，保持null。[初步开发基线与数值草案](P1-16-development-baseline.md)、[最终汇总](../verification/2026-10-29-P1-16-development-review-complete.json)及[完整性验证](../verification/2026-10-29-P1-16-development-review-complete-integrity.json)保存全部分层结果。

F-150 C006人工理由的“零召回”与实际matched 2、投诉题人工理由的“挂起”与实际COMPLETED均分别保存，不静默改原话、原答案或生命周期。新增8格评价仍来自同一人，不声称多审查者一致性；此前2份输出/12条结论另记，累计10个任务输出、28条生成结论，不跨集合计算总体成功率。

## 继续审查入口与限制

三份实际回答及五项停止任务的原始审查JSON保存在本批`.local/b16/17a29b947517/l/review-packets/`，不含参考标签。已审的原Markdown与JSON哈希固定，后续渲染不会覆盖它。第二份阅读包为BLIND-95cee9ebfdee-reading.md；第三份阅读包为BLIND-adbcda2666d8-reading-v2.md，原始完整Markdown/JSON仍保留。阅读版只省略重复定位元数据，并列出关系类型、条件/未知字段和来源事实，不新增评分。

完整标签由实现代理编写，独立审查尚未覆盖整套义务。`prius_symptom`等定位仅为原文锚点，不能按字符串命中代替语义；关系推断需要对照两机制及适用范围。任何开发侧细化都建新版本并保留本轮，下一版未见测试在目标/标签固定后再执行。[验收修订建议](P1-16-acceptance-revision.md)与完整项目目标仍有效。

无新请求的复核入口：

```powershell
uv run --locked --offline python -m scripts.audit_development_live --plan .local/P1-16/development-baseline/17a29b9475174de0b5277f6e8259fcb1353e25c57cce314662378380a224dde4.json --summary .local/b16/17a29b947517/l/summary-e6f686ada2dcd5992b019ccacd6c52eafd8af5ef04ce1413fe08785d97cca8a4.json --output .local/P1-16/rechecked-development-review-complete.json
```

上述读取本机已保存资料，不会调用模型；新机器仍需固定发布/来源/本地工件。公开打包、干净复现与GitHub发布尚未实施。P1-16剩余独立开发基线/数值目标和全批语义审查未全部完成，P1-17未开始。
