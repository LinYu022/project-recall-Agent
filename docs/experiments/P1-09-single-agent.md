# P1-09：单 Agent 对照与验收记录

日期：2026-10-27。同步单 Agent、工程验收及获批的真实开发对照已交付。本轮没有证明 Agent 优于固定流程，默认方法仍为 fixed。独立人工审查尚未进行，正式语义指标为 null；P1-10 未开始。

## 共同条件与实验分类

依赖 P1-07 的固定调查、证据校验与评分，以及 P1-08 的授权和状态。两道题是已经看过的开发案例 DEV-PRIUS-JOINT、DEV-DOOR-PAIR，不是未见测试。固定发布为 `19286b24cd6d539155c61abfa1265d256dd7c5e5c3b4229eaa004782f8393ee6`，模型为用户选择的 DeepSeek `deepseek-flash`，复用同一工具能力、问题和 v0.2 参考；标签及历史答案不进入模型输入。

[共同配置](../../examples/single-agent-comparison.json) 规定每题每方法最多 6 次工具、6 次模型、2 次展开、每次 24,000/累计 96,000 输入字符、每次 2,000/累计 4,000 输出 tokens、900 秒。固定流程仍按原策略生成一次，其余额度不是虚构的已消费量。P1-07 历史默认与结果保留，第一次新对照使用这套共同上限。

记录分为四类：合成故障测试；官方冻结资料上的脚本决策/模拟回答；真实 API 调用；相同实际输入的已保存响应回放。真实资料与真实数据库不能把模拟模型变成真实模型实验。

## 工程验证

最终 331 项无数据库测试、98 项真实 PostgreSQL 集成测试通过，失败和错误均为 0；Ruff、76 个源码文件 mypy、迁移 SQL 生成、旧两组各 7 项回环 HTTP 检查通过。没有新依赖、迁移或来源请求。临时 PostgreSQL、HTTP 服务与实验 schema 均已清理。

覆盖 Decision 白名单、候选展开前置条件、全集零结果护栏、改写查询、假设/反证保留、有限超时重试、重试耗尽与成功空结果区分、重复/无进展停止、模型/工具/字符/生成/时间上限、缺 usage 保守预留、无效引用拒绝和授权撤销屏障。新增诊断测试验证无效 JSON 的原始内容与用量留档，以及跨脚本重启的请求预留、并发锁和失败回放。

[旧工程快照](../verification/2026-10-27-P1-09-engineering.json) 是真实调用前的 325/97 测试与待批准历史记录，不代表当前状态。当前机器摘要为 [P1-09-results.json](P1-09-results.json)。

## 官方资料上的模拟对照

以下是早期四次受保护运行，使用真实 PostgreSQL、HTTP 和既有 NHTSA 冻结资料，但由脚本选择工具并返回证据不足。

| 案例 | 方法 | 模拟模型调用 | 工具尝试 | 实际构造的输入字符 | 执行结果 |
| --- | --- | ---: | ---: | ---: | --- |
| Prius 联合证据 | 固定流程 | 1 | 4 | 23,965 | completed / insufficient_evidence |
| Prius 联合证据 | 单 Agent | 4 | 4 | 45,524 | completed / insufficient_evidence |
| 车门两次召回 | 固定流程 | 1 | 5 | 16,254 | completed / insufficient_evidence |
| 车门两次召回 | 单 Agent | 5 | 5 | 75,793 | completed / insufficient_evidence |

字符数用于检验资源机制；fixture 的 40 等合成 token 数不是 provider 用量，不能汇入真实费用或质量指标。

## 真实调用：三轮全部保留

此前自动审批认为 P1-07 授权未覆盖本次资料发送，在发送前拒绝。用户随后明确批准 [两个案例及最多 14 次请求](P1-09-live-call-proposal.md)，取得批准后才执行。实际共 12 次，剩余 2 次不再使用，不通过继续生成追求通过。

| 轮次 | 新请求 | 固定响应回放 | Agent 观察与结果 |
| --- | ---: | ---: | --- |
| 首轮共同上限对照 | 4 | 0 | 两个固定回答通过程序校验；Prius Agent 的内容不是合法 JSON；车门 Agent 提前 finish，把召回 lookup 卡用于 count 声明，被拒绝 |
| 决策协议修订 | 2 | 2 | 改为独立的 Decision 优先提示词；两 Agent 返回 JSON，但用来源 ID 代替 D001 等候选别名展开，均被 Schema 拒绝 |
| 候选前置条件修订 | 6 | 2 | 无候选时只开放检索，加入 D/E 用途说明；Prius Agent 触发输入上限，车门 Agent 的最终声明未通过证据类型校验 |

提示词从原始 `bounded-react/0.1` 修订到 `bounded-react/0.3-candidate-preconditions`。检查发现最初把最终回答提示和决策提示拼接，顶层输出要求存在冲突，因此改为独立决策协议；这不是对首次无效JSON唯一成因的证明。修订保留错误记录，不回填成成功。现适配器对内容 JSON 解析失败保存原始内容、finish_reason、模型身份及 usage；第一次无效 JSON 是旧版行为，当时未保存，无法补造。

后两轮是开发修复，不能称为预注册、独立或全新等预算质量对照：第二轮模型调用上限降为 5，第三轮降为 4；第三轮 Agent 输出剩余额度分别为 Prius 1,765、车门 3,208 tokens，固定回答沿用原响应。跨三轮每题 Agent 输入字符为 26,433 和 56,650，均未超过 96,000；未知历史输出按 2,000 tokens 保守预留。落盘 RequestAllowance 在发送前扣请求额度，失败与在途调用都计入，不自动重试，也不静默重建已有锁。

### 所选最终运行

固定回答实际生成于首轮，表中的用量只计这一次生成；第三轮读取原响应没有新增固定请求。工具数包含程序先执行的全集 lookup。耗时为本机整个运行，包含资料校验/审计，并非纯 provider 延迟。

| 案例 / 方法 | 模型 / 工具 | 输入字符 | 输入 / 输出 / 总 tokens | 耗时 ms | 结果 |
| --- | ---: | ---: | --- | ---: | --- |
| Prius / 固定 | 1 / 4 | 23,965 | 8,435 / 683 / 9,118 | 29,018.26（首轮） | completed / answered；语义待审 |
| Prius / Agent | 2 / 3 | 14,734 | 3,827 / 431 / 4,258 | 19,956.84 | completed / insufficient_evidence；prompt_budget_exhausted |
| 车门 / 固定 | 1 / 5 | 16,254 | 5,021 / 580 / 5,601 | 40,315.30（首轮） | completed / answered；语义待审 |
| 车门 / Agent | 4 / 4 | 44,677 | 12,910 / 1,269 / 14,179 | 30,352.14 | failed / ValueError；最终回答被拒绝 |

Prius Agent 选择 search 后 expand D001、include_related=true，累计获得 24 张证据卡；构造下一轮输入时超过单次 24,000 字符，未发送第三次请求，保留证据并说明不足。表中字符数是已调用模型的输入，不含被阻断的下一包。

车门 Agent 依次 search、expand D002（false）、expand D006（false）、finish，获得 11 张卡；其 relationship 声明只引用文字卡，缺少 links 产生的结构化关系卡，被同一校验器拒绝。另有 scope_exclusion 同样只引文字卡的问题。没有放松校验、把失败替换为通过或将原始拒绝稿作为正式答案。具体措辞争议见 [实现代理审查草稿](P1-09-review-draft.md)。

全部真实请求按题/方法统计：Prius 固定 1、Agent 4；车门固定 1、Agent 6。11 次已知 usage 合计输入 34,596、输出 3,990、总计 38,586 tokens。第 2 次请求（首轮 Prius Agent）usage 未知，所以完整总用量和费用均为 null。后续回放响应所携带的历史 usage 不重复累加到新增请求用量。

本轮没有测得 Agent 在相同回答质量下的收益。Prius 的低用量来自提前停止；车门消耗更多真实 tokens，仍未交付合格回答。运行总耗时受审计、缓存和本机负载影响，不能由上述单次数字推断效率优势。

## 零新增请求回放

最终回放读取 8 份已保存响应：两个固定回答、Prius 两轮决策、车门四轮决策。新增 API 请求为 0，逐项匹配所选运行的 execution_status、error_code、disposition、模型/工具数和 termination，包含同样的证据不足及最终拒绝。回放通过表示重现结果，不表示两个 Agent 都成功回答。

回放按原报告的内容哈希和不可变工件读取每题实际配置，避免后续覆盖 `reports/` 视图时错用配置。实际 provider 输入仅将不影响业务的内部任务/修订 ID 规范化；真实发送和回放执行相同转换，来源、范围和原文不变。输入、输出、报告与逐轮配置/哈希均留档。

```powershell
# 需要本机已保存的冻结包、真实响应和原报告；没有记录即失败，不退回联网。
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --agent-checks --replay-model --agent-profile-record .local/P1-09/live-final.json --agent-model-call-cap 4 --output .local/P1-09/replay-final.json
# 只读本地工件并导出当前公开摘要，不调用模型。
uv run --locked --offline python -m scripts.export_single_agent_results
```

`.local/P1-09/live-round1/`、`live-round2/`、`live-round3/` 保存原轮次；`request-allowance.json` 保存请求账本，`recorded-model/` 保存响应，`portable-model-inputs/` 保存实际发送输入，`runs/private-runs/` 保存不可变工件。它们不进入 Git；公开 JSON 提供摘要及哈希，不能单靠公开摘要伪称重现过旧 provider 响应。没有本地材料的机器可先运行合成回归；新资料获取与新的真实请求需另记结果。最终干净环境交付仍属于 P1-18。

## 审查边界与计划建议

独立人工审查 0 份，正式语义指标 null。实现代理已检查部分措辞并登记草稿争议，但没有充当独立人类裁决。固定回答通过机械校验也不自动成为金标准；盲审导出与现有评分工具保留，后续正式评价仍需补独立审查。

保持 fixed 默认；进入 P1-10 后按原计划实现上下文选择、工件分页、保留条件/反证的结构化摘要及原文回查，优先解决已经实际触发的输入超限。后续评价应先冻结协议，再用未见案例运行共同上限比较；不能把这次调提示的过程包装成泛化收益。后台恢复、多 Agent、记忆、综合实验和 GitHub 发布全部保留；本次交付后等待理解确认。
