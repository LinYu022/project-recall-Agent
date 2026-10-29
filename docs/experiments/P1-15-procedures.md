# P1-15：程序性记忆、迁移验证与版本验收

2026-10-28（Asia/Shanghai）。本模块验证有界流程能否从可回放经历形成候选、在不同任务/事件上验证、被授权采用并在失效后停止。无新增模型API或来源下载请求，P1-09的12次请求账本保持不变，不消费剩余许可。实际新provider用量/费用与正式语义指标为null，独立人工语义审查0。

## 真实与模拟

| 层次 | 真实执行 | 模拟部分 | 结论边界 |
| --- | --- | --- | --- |
| 契约/变异 | 严格Schema、编译、前置条件判断 | 合成字段与恶意输入 | 不可编写任意工具/代码，缺硬约束拒绝 |
| PostgreSQL专项 | 认证API、事务、实际工具、哈希工件、Journal恢复、状态转换 | 合成来源、脚本生成、阻塞/中断注入 | 生命周期、隔离、预算和失效传播接线 |
| 官方迁移 | 固定NHTSA资料、真实本地检索模型、SQL、工具、原文回放 | 来源任务最终提案、审核者、比较决策为脚本 | 配方可在来源之外的事件完成范围检查 |
| 旧响应回放 | P1-09保存的8份真实响应与原输入 | 本轮不生成 | 原结果仍可精确复现 |

API专项使用进程内FastAPI TestClient；回环HTTP另有旧两组检查。中断恢复为同进程故障注入，不冒称新增独立进程强杀实验。采用记录只证明Skill被选入或执行，不证明提高了答案质量。

## 官方演示与比较设计

固定expanded release：`19286b24cd6d539155c61abfa1265d256dd7c5e5c3b4229eaa004782f8393ee6`。来源经历ORIGIN-PRIUS限定TOYOTA/PRIUS/2010、10V039000；ORIGIN-FORD限定FORD/F-250/2017、17V331000。两者实际lookup/search/expand并回放，脚本最终返回insufficient_evidence，不伪称个车适用或真实答案已被人工证实。

候选定义只带当前任务参数和有限动作，不带来源答案。HELDOUT-TESLA限定TESLA/MODEL 3/2018、23V838000，DRAFT/PLAN模式；真实执行3次只读工具，迁移新增生成调用0，检查前/后条件后成为validated。已知福特关联召回按事件族合并，不只按任务数或来源文件数计经验。

这里的heldout/未见只相对于该候选的来源经历。冻结包属于已有开发资料，不是项目从未查看过的正式测试集；本轮没有建立或测量P1-16正式未见集。

TEXT-TESLA与SKILL-TESLA共用同范围/来源/检索配置和调用上限。前者Plan.steps包含普通经验文字，后者显式选择版本。脚本按预定策略执行，没有真实模型学习或盲评；两者输入内容会因定义/建议而不同，并逐次记录输入字符。

共同配置为hybrid、retrieve_k=4、expand_k=1、structured、最多6工具/4生成、单次32000/累计96000输入字符、单次2000/累计4000输出单位、900秒。模拟usage固定40单位/次且明确标注synthetic，不当作provider真实tokens。模型身份为scripted-procedure-comparison。

预定普通策略search→expand→finish为3次模拟决策；Skill策略apply_skill→finish为2次。二者实际工具均为lookup/search/expand共3次。只有两者termination都为agent_finished才视为此项演示完成；提前耗尽预算不能混为等价完成。范围检查结果仍保留individual_applicability与causality为not_confirmed，最终均为不足。

删除year前置条件只做选择谓词变异：不完整条件可能误返回无缺项，但compile_procedure必须拒绝，实际不安全工具调用为0。末尾显式revoke版本，旧报告的动态skill_audit显示unavailable，不改原报告。版本切换/rollback在数据库专项独立覆盖。

## 失败轮次与修订

- initial.json：11项数据库测试全部在fixture准备报错。合成记录改为新campaign，但合成SourceSpec仍选择旧编号，正式导入正确拒绝campaign_invalid_or_outside_selection。修正测试来源绑定，没有放宽生产校验。
- round2.json：原11项全部通过。
- round3.json：18项中17通过、1失败。停用来源episode后已阻断使用，但以普通ValueError使任务FAILED；改为AccessDenied使依赖失效任务PAUSED，正式报告仍不发布。
- round4.json：18项全部通过；官方来源/迁移及四份任务已执行，比较脚本断言失败。模拟模型未报告usage，按每次2000预留使普通流程在两次生成后completion_budget_exhausted，并非预期第三次finish。该轮不算比较通过，保留原报告和失败状态。
- 后续脚本显式报告synthetic usage并校验termination，同时修正演示的报告引用：使用内部已核验工件指针，而非公开view仅返回的sha256；内容哈希针对原不可变报告计算，动态审计单独记录。导出器从首轮保留哈希找回原报告，不回写旧轮记录。

增加未知中断留running/显式abort、在途abort迟到不晋升、目标事件去重、来源/目标/证明失效、未知年份、旧报告动态审计及Dispatcher预算旁路检查。代码指纹改变即旧证明过时；最终验证前固定实现，不将旧验证自动迁移到新代码。

## 复现与最终记录

```powershell
uv run --locked --offline python -m scripts.verify_offline --output .local/P1-15/offline-final.json
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --procedure-checks --agent-checks --replay-model --agent-profile-record .local/P1-09/live-final.json --agent-model-call-cap 4 --output .local/P1-15/final.json
uv run --locked --offline python -m scripts.export_procedure_verification
```

无数据库检查与数据库fixture不需要官方资料/模型密钥。官方演示和旧回放需本机已冻结包、权重与旧响应；缺失明确失败，不联网补抓。迁移接口有1～4目标/每目标3工具的上限；既有Dispatcher根任务拒绝在验证旁路另领额度。意外中断不自动重试，不把已在途操作计为未发生。

最终观察由[机器摘要](../verification/2026-10-28-P1-15-procedures.json)从本机原记录和哈希工件导出，记录全量与筛选专项、失败轮次、官方报告、旧回放与清理结果。新机器生成自己的记录；仓库摘要不是独立复现证明。用量/延迟仅作本轮观测，包含脚本差异与本地核验开销，不计算质量提升或加速比。

最终418项无数据库、191项真实PostgreSQL检查通过；14项契约/19项程序专项分别为总数子集。106源码mypy、Ruff、迁移SQL与旧两组各7项HTTP通过，旧8份真实响应精确回放结果匹配。最终官方脚本、回放与全部数据库退出0，PostgreSQL/回放HTTP停止、实验schema清理。代码/定义/报告/证明及旧账本哈希已核对，原计划与uv.lock不变。

| 最终官方任务 | 模拟生成 / 实际工具 | 输入字符，含system | 调查器秒数 | 结果 |
| --- | --- | --- | --- | --- |
| ORIGIN-PRIUS | 1 / 3 | 6587 | 16.89 | 不足，可回放流程 |
| ORIGIN-FORD | 1 / 3 | 6758 | 16.60 | 不足，可回放流程 |
| TEXT-TESLA | 3 / 3 | 37919 | 26.29 | agent_finished，不足 |
| SKILL-TESLA | 2 / 3 | 28224 | 35.15 | agent_finished，不足 |

目标迁移验证另计3次实际工具、0生成；通过后才启用。最终少一次模拟决策的Skill调查器耗时仍更高，不能把调用数差异当加速；额外证明/来源检查也有开销。四份最终报告、独立验证证明和首轮失败报告引用均保留于机器摘要。

model_input_characters_including_system是本地对每次system及payload JSON序列化后的字符计数，不是provider tokens。报告elapsed_ms是调查器内部耗时，未包括调用前SkillBinding/发布准备的全部开销；不能拿这列声称端到端加速。

## 计划口径与保留目标

建议把计划中的“成功流程”明确为“满足预先指定前后条件、证据可回放的过程”，并另列“答案语义经独立评价”。否则COMPLETED或不足的任务容易被误当成业务成功。本模块实现前者的范围检查配方，不声称后者已完成；原计划未改写、目标未删减。

事件族登记只覆盖已知关联，未登记的现实事件关联仍可能遗漏；当前不声称通用独立性分类器。候选提取为确定性有界配方匹配，不宣称模型从任意轨迹自动发明可执行代码。普通文字经验/Skill的真实模型质量、成本和适用性对照、外部干扰及独立人工审查由P1-16完整保留。

停在P1-15等待理解确认。P1-16综合评测、P1-17产品及P1-18干净环境/GitHub发布仍需继续；当前没有Git远端、尚未上传。
