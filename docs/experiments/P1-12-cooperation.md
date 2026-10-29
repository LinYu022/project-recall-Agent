# P1-12：协作机制与真实资料演示

日期：2026-10-27。目标是让四种调查方法在相同根额度下可运行，验证独立上下文、共享额度、恢复、部分失败和冲突保留。全部新增生成明确为模拟；模型API请求0、来源请求0。实际provider用量/费用、正式语义指标为null，独立人工审查0。P1-09获批范围与12次请求账本保持不变。

## 实验分层

| 检查 | 真实部分 | 模拟/注入部分 | 结论边界 |
| --- | --- | --- | --- |
| 无数据库检查 | Python契约、哈希工件、Qdrant local | 合成向量、Schema输入 | 角色收窄、无投票、目录排队、原子工件发布 |
| 13项协作数据库检查 | PostgreSQL事务/锁、线程、工具链、持久调用记录 | 小型fixture来源、脚本模型、阻塞/崩溃/过期注入 | 隔离、并发推进、取消、部分结果、未知预留、lane恢复与额度 |
| 官方资料四方法演示 | 已保留的NHTSA文件、正式DatasetRelease、本地检索模型、原文引用、PostgreSQL和线程 | 同一脚本生成器返回有界不足，usage为合成40单位/次 | 真实资料可达、四方法接线和实际工程开销；不支持回答质量或收费结论 |
| P1-09精确响应回放 | 历史真实provider响应和官方固定资料 | 无新生成 | 历史成功/不足/失败结果仍可复现，不能代表新协作质量 |

测试中的worker崩溃使用InjectedCrash，在同一测试进程内模拟退出；P1-11另有真实独立worker进程退出实验，本轮不把两者混称。

## 同条件设置

两个已看过开发案例：DEV-PRIUS-JOINT（Prius召回API与PE10006结案PDF）、DEV-DOOR-PAIR（两个车门召回API）。固定release：`19286b24cd6d539155c61abfa1265d256dd7c5e5c3b4229eaa004782f8393ee6`。允许来源、问题、范围和参考评分相同，参考标签只在生成后读取。

四方法为fixed、single_agent、parallel_tools、multi_agent。共用`examples/cooperation-comparison.json`：hybrid检索、top6、最多两次展开，8次模型/10次工具、单次24000及累计96000输入字符、单次2000及累计4000输出单位、900秒。固定方法仍按原步骤运行，可能用不到全部上限。多Agent在根额度中切分，分支份额详见[架构](../architecture/cooperation-v0.1.md)，不能各自得到一份完整上限。

该8/10配置是本模块四方法共同的工程演示配置，不改写P1-09历史6/6配置，也不代表取得任何新模型请求许可。角色工具收窄是策略差异；没有提供更多来源或额外工具能力。各方法输入组织及专门提示不同，正是被比较的机制。

## 开发故障及修正历史

1. 最初5项协作数据库检查通过；扩展到8项时1项失败：并行工具fixture错误地把同一记录作为关系两端，真实查询拒绝。改为显式模拟unsupported关系结果，该检查只用于并发调度，不冒充真实关系判断。随后9项通过。
2. 首轮两个真实案例×四方法全部父任务交付，但两次multi均有分支RuntimeError。Prius的DATA失败，DOCUMENT触发局部输入耗尽；车门的DOCUMENT失败，DATA触发局部输入耗尽。记录完整保留，没有将父COMPLETED解读成全部分支成功。
3. 单独诊断Prius，DATA完成而DOCUMENT仍RuntimeError。私有堆栈定位到冻结包完整性校验同时打开Qdrant local，同目录不允许两个客户端。数据分支也会校验向量组件，仅复制服务锁不足。新增按目录的完整生命周期锁，保留全部向量/发布校验；同目录访问序列化，其他工作仍能重叠。
4. 同时修复共享哈希工件的半写可见问题：先完成临时文件，再原子发布；8线程同一内容写入及损坏拒绝检查通过。没有更改工件编码或旧哈希。
5. 扩展到13项协作数据库检查时1项失败：恢复测试错误地把原文完整性复核也当成重新调度。修正为核对持久账本的新工具次数及重放次数，保留必要原文回查。多分支/汇总已保存响应的恢复、并行batch恢复和每lane不可借用兄弟额度均通过最终全量检查。
6. 修正启动额度阈值：累计输入至少30000字符才能按现有比例满足每分支5000字符的Schema下限。低于阈值退回单Agent；不等到分配时抛Schema错误。

首轮Prius文档分支已用40043输入字符，前三次输入分别10077/12889/17077，下一轮无法容纳必需上下文；首轮车门数据分支首轮输入10963，局部16000总额不足再完成一轮。它们是保留的预算负结果，不是Qdrant错误。最终不通过只增加multi总额来消除这些结果。

首轮8份及诊断1份原父报告均已从不可变哈希工件找回，并留存`.local/P1-12/rounds/pre-storage-fix/immutable-reports/`；公开机器摘要记录9份哈希和定位索引。后续每次演示创建独立run_group_id及报告目录，额外记录正式发布工件引用、分支实际配置和终止原因，避免可覆盖便利副本掩盖旧结果。

## 当前验收

完整377项无数据库、132项真实PostgreSQL测试均通过；Ruff、86个源码文件mypy、迁移SQL和两组各7项回环HTTP检查通过。13项协作数据库检查是132项的子集，22项契约/向量/工件练习检查是377项的子集，不额外相加。旧8份provider响应精确回放成功；临时PostgreSQL/回放HTTP均停止，实验schema已删除。

| 案例 | 方法 | 模型/工具次数 | 本次调查耗时（秒） | 父交付/最终结论 |
| --- | --- | --- | --- | --- |
| Prius | fixed | 1 / 4 | 21.34 | completed / insufficient_evidence |
| Prius | single_agent | 4 / 4 | 21.60 | completed / insufficient_evidence |
| Prius | parallel_tools | 4 / 4 | 21.55 | completed / insufficient_evidence |
| Prius | multi_agent | 5 / 4 | 26.24 | completed / insufficient_evidence |
| 车门 | fixed | 1 / 5 | 31.74 | completed / insufficient_evidence |
| 车门 | single_agent | 4 / 5 | 47.34 | completed / insufficient_evidence |
| 车门 | parallel_tools | 3 / 5 | 41.40 | completed / insufficient_evidence |
| 车门 | multi_agent | 5 / 5 | 40.21 | completed / insufficient_evidence |

最终两次multi的四个分支均完成证据保全交付，没有重现RuntimeError。Prius DATA以agent_finished结束，DOCUMENT因prompt_budget_exhausted停止；车门DATA/DOCUMENT均因prompt_budget_exhausted停止。它们的completed表示正常保存局部成果，不能读成调查目标全部完成或模型得出正确答案。两份主报告均保留不足；没有增加总额度去填平这些负结果。

最终机器摘要见[2026-10-27-P1-12-cooperation.json](../verification/2026-10-27-P1-12-cooperation.json)，按案例逐项列出父交付、分支状态/终止、实际模型/工具次数、资源和单次耗时。模型usage为模拟数据，计数不代表真实provider token；原文复核另计，不与新增收费请求混加。

官方来源示例不能证明多源调查质量收益：两个案例已见过，样本极小，模型始终脚本生成，模拟不足也不会变成高质量人工答案。单次耗时含完整发布校验、模型载入及本地磁盘竞争，既无重复实验也无稳定缓存控制，不能据此报告加速比。默认继续保留fixed，真正模型主对照、消融及独立语义审查仍按完整P1-16目标推进。

## 复现

```powershell
uv run --locked --offline python -m scripts.verify_offline --output .local/P1-12/offline-final.json
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --cooperation-checks --agent-checks --replay-model --agent-profile-record .local/P1-09/live-final.json --agent-model-call-cap 4 --output .local/P1-12/final.json
uv run --locked --offline python -m scripts.export_cooperation_verification
```

完整来源检查与旧响应回放需要本机已有的P1-06冻结包、模型文件和P1-09记录，缺失时明确报错，不自动下载替代品。摘要导出器还需要开发轮次和保留的9份旧报告，核验原实验历史；新机器没有这些历史时可运行前两条生成自己的工程记录，不应伪造本机历史摘要。无真实缓存也可运行全部无数据库检查和数据库fixture组；首次安装依赖/准备资料按前置模块操作。

## 对计划的说明与修订建议

原计划保持原字节，不删功能。实现中发现三个需要明确的验收口径：

- “主Agent回查原文”可验证引用/条件的保留，不能仅凭程序校验推导语义裁决。当前保守保留冲突，建议未来另加可审计的语义判据或独立审查，避免把冲突表为空当成语义正确。
- “四方法同条件运行”与“真实模型质量比较”分层验收。本模块完成机制和官方资料可用性演示；真实provider质量/成本比较仍完整保留到P1-16，并受具体资料发送授权约束。
- 本地Qdrant限制同目录客户端并发。当前协调访问可保证正确性，建议只有测量证明需要时再评估向量服务部署，不能在学习模块中静默改变后端或宣称吞吐收益。

练习与参考答案见[P1-12学习记录](../learning/P1-12.md)。本轮结束等待理解确认，再进入P1-13。
