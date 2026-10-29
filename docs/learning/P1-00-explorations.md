# P1-00 人工探索记录

日期：2026-10-26（Asia/Shanghai）。10 个任务、10 个主召回编号；这不是统计独立性证明。均为探索材料，不进入未见测试集。

人工阅读字段与资料后编写参考结论；字段定位和哈希复核由程序辅助。没有运行调查 Agent、检索评测或真实模型实验。原始文件仅在 `.local/exploration/raw/` 保存；获取入口及本次字节校验值见 `docs/data/exploration-sources.json`。

## EXP-001：跨源关联与直接查询

问题：核对 2010 TOYOTA PRIUS 的 10V039000 记录与 PE10006 结案文件：确认文件实际说明的关系，区分车型记录数、召回事件数和个车适用性。

参考结论：API 的两条车型记录共有一个召回编号；TOYOTA PRIUS 2010 记录匹配。PE10006 的 Action 明确引用 10V-039，Summary 将结案与 Toyota 的召回行动联系起来，并保留继续监测。不能由此断言所有投诉已被逐一解释或所有车辆问题已消除。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：两条车型记录是两次召回；结案证明所有 Prius 投诉都已解决。

证据位置：NHTSA-API-CAMPAIGN-10V039000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-INV-PE10006-CLOSE / page=1; Action; Summary

未知项：具体车辆的生产与配置范围、具体投诉的因果。

## EXP-002：跨源风险与修复核对

问题：核对 23V162000 的 2013 FORD FUSION 制动软管记录和官方托管的车主通知：两份资料能联合支持哪些症状、风险与修复内容？

参考结论：API 列出 2013 Fusion，主题是前制动软管破裂和制动液泄漏；通知第 1 页说明制动踏板行程增加、减速能力降低和更换前软管。两者联合支持该车型层面的召回描述；样板通知中的个车措辞不能转移到当前用户车辆。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：只要是 2013 Fusion，当前用户这辆车就一定需要本次修复。

证据位置：NHTSA-API-CAMPAIGN-23V162000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-OWNER-23V162 / page=1; recall heading; issue/risk/repair

未知项：具体车辆是否适用、既往修复情况。

## EXP-003：条件歧义

问题：只知道车辆是 2008 FORD ESCAPE，能否确认适用 14V284000？请列出源记录中的额外范围条件。

参考结论：记录涉及某些 2008–2011 Escape/Mariner，Summary 还限定生产日期为 2006-08-18 至 2010-09-11。只知道 2008 车型年仍不足以独立证明任意个车属于受影响集合，应补充生产日期及适用范围核对。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：车型年为 2008，所以生产日期已知且一定适用。

证据位置：NHTSA-API-CAMPAIGN-14V284000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy

未知项：生产日期、个车范围证明。

## EXP-004：投诉线索与证据不足

问题：读取 ODI 11766023 的投诉及 20V332000：投诉提及 20S31，是否足以确认本车缺陷、适用性和投诉中的全部说法？

参考结论：ODI 11766023 的投诉人提及 20S31 和制动主缸。官方 20V332000 的 Remedy 使用 20S31 编号，Summary 限定某些 2014–2017 F-150 且配备 3.5L EcoBoost。可以记录投诉自述及编号关联线索，不能独立确认其车辆配置、故障或全部陈述属实。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：投诉人说有召回，所以其车辆缺陷和对厂商的法律指控都已得到官方确认。

证据位置：NHTSA-API-CAMPAIGN-20V332000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-COMPLAINTS-F150-2016 / results[odiNumber=11766023].summary; products

未知项：发动机配置、车辆适用性、零件供应说法的独立核实。

## EXP-005：错误关联反例

问题：18V467000 的车型与 2018 TIGUAN 相符，是否可以把它当作制动失效证据？这能否说明该车型没有制动缺陷？

参考结论：该记录描述全景天窗环境灯 LED 模块短路，Component 属于天窗装置。车型匹配不能把它变为制动证据；本题只核对这一召回，不能由排除这一条推出没有其他召回或缺陷。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：有同车型召回就解释刹车问题；或者这一条不是刹车召回所以没有刹车缺陷。

证据位置：NHTSA-API-CAMPAIGN-18V467000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy

未知项：其他制动记录是否存在、具体投诉机制。

## EXP-006：统计分母与证据不足

问题：13V489000 提供潜在受影响数量，能否直接计算 2009 GENESIS 的投诉故障率？请说明记录实际支持的缺陷范围。

参考结论：官方描述某些 2009–2012 Genesis 的原制动液可能未能防止 HECU 腐蚀，并给出检查及处理方案。潜在受影响数量不是本题已经核对的投诉统计总体；没有投诉分子、统一观察期和适当分母，不能报告故障率。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：潜在受影响车辆数就是发生故障车辆数，故障率为 100%。

证据位置：NHTSA-API-CAMPAIGN-13V489000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy

未知项：适当分母、同口径投诉分子、去重与观察期。

## EXP-007：历史时点与后续修订

问题：要求仅使用 2014-02-15 当时已公开的具体版本，还原 14V047000 的范围；今天下载的响应是否足够？

参考结论：当前 Summary 同时描述 2014-02-10、02-25 和 03-27 的通知与扩展。因此该响应包含知识截止之后的信息；本次没有取得历史归档版本及公开证明，应返回当前资料不足以支持该历史时点还原，而非照抄当前完整范围。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：记录的最早日期在截止之前，所以整个当前响应都可以用于历史回答。

证据位置：NHTSA-API-CAMPAIGN-14V047000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy

未知项：截止前具体内容版本及公开证明。

## EXP-008：同车型不同缺陷

问题：16V487000 涉及 2010 PRIUS，能否据此解释与 10V039000 类似的制动症状？

参考结论：16V487000 描述侧气帘充气装置焊缝可能开裂，与 10V039000 的 ABS 制动主题不同。车型范围交集不是症状或因果证明；该气帘记录不能单独解释制动投诉。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：同为 2010 Prius，所以气帘召回证明刹车投诉属于同一缺陷。

证据位置：NHTSA-API-CAMPAIGN-16V487000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-API-CAMPAIGN-10V039000 / results[*].Summary; Component

未知项：具体制动投诉的有效关联证据。

## EXP-009：来源定位与范围核对

问题：核对 19V645000 API 与官方确认函中的 Silverado 1500 范围、真空助力风险和修复措施；这些资料能否证明任意车辆已完成修复？

参考结论：API 与确认函均涉及部分 2014–2018 Silverado，确认函车型列表明确 Silverado 1500。问题是真空泵提供的真空随时间减弱，可能降低制动助力；文件给出电子制动控制模块重编程方案。这是方案证据，不是某辆车已维修或风险已消除的证明。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：官方列出了修复办法，所以所有涉及车辆都已经修好。

证据位置：NHTSA-API-CAMPAIGN-19V645000 / results[0].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-ACK-19V645 / pages=1-2; Makes/Models; Problem; Consequence; Remedy

未知项：个车适用性、修复完成情况。

## EXP-010：相似症状但范围不符

问题：2016 FORD EDGE 前制动软管问题能否直接套用 23V162000 的 Fusion/MKZ 召回？请与 20V469000 区分。

参考结论：20V469000 列出某些 2015–2018 Edge 与 2016–2018 MKX，主题是前制动软管过早破裂；23V162000 涉及 Fusion/MKZ。两者症状相近仍不能把 Fusion 的范围套到 Edge；2016 Edge 只能形成与其范围匹配的候选，具体车辆仍待核查。

手工路径：明确范围 → 查看原始记录及定位字段 → 保留条件 → 核对附加来源 → 写有限结论和未知项。

有意义的失败：都叫前制动软管破裂，因此两次召回适用车辆可以互换。

证据位置：NHTSA-API-CAMPAIGN-20V469000 / results[3].Make/Model/ModelYear/NHTSACampaignNumber/Summary/Remedy；NHTSA-API-CAMPAIGN-23V162000 / results[*].Make/Model/ModelYear; Summary

未知项：具体车辆的适用性与修复状态。
