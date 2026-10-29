# 程序性记忆与可验证 Skill v0.1

P1-15复用P1-13的用户/项目空间、撤销与审计，以及P1-14的任务经历和原始证据回查。这里的Skill是调查应用内的有界流程对象，存于数据库；不是Codex个人技能，不执行自由文本代码。

## 可信执行边界

当前安装一个范围检查配方：`lookup → search → expand`。参数来自当前任务的scope、query、release和允许来源，不能由历史答案、候选文本或模型补入新权限。动作、允许工具、失败策略及后置条件是有限枚举；生产编译器要求保留全部强制条件。

前置条件要求make/model/model_year、支持的意图及release。applicability还要求生产日期和配置。后置条件要求原文重放、保留范围上下文，以及个车适用性仍未确认。流程返回scope_inspected只表示完成范围检查，不是最终答案成功；空结果只说明允许快照的查询结果，不证明现实世界没有召回。

检索优先选择matched，未匹配时可保留pending，但pending不能通过迁移验收；只展开一个结果，include_related=False。当前实现不支持任意循环、脚本、外部URL、模型生成工具名或凭证参数。要增加配方须修改可信代码、版本化并重新验证。

## 候选与迁移

`ProcedureService.candidate()`从当前调用者拥有的已启用episode中提取流程出处。出处需要COMPLETED、报告/证据校验成功，并包含lookup/search/expand。每次核对原任务授权、不可变报告及原始来源；不读取旧回答来生成当前事实。

单个成功经历可形成candidate，但不能启用。验证要求至少两个互不重叠的来源事件族，以及至少一个来源任务之外、事件族也未出现的目标。已知关联召回由`examples/procedure-event-families.json`合并；未登记编号退回campaign身份，不能据此宣称已识别所有现实事件关联。多目标也不能重复计同一事件。

验证仅接受DRAFT、PLAN模式、同项目的已授权research任务；已进入Dispatcher根预算池的任务拒绝借验证旁路再分配额度。每目标最多3次实际工具调用、每批1～4目标、总计最多12次，无生成模型。工具调用前先持久预留，工件保存完整结果、当前证据、事件身份、定义哈希和执行器代码指纹。

同一版本的验证请求按幂等键和完整输入哈希去重；相同键不同输入冲突。意外中断保留running记录，重复请求不重发操作。已认证调用者可查询并显式abort，记失败原因；已在途的只读操作可结束，但不得继续工具调用或让迟到结果晋升版本。预期错误保存failed证明，所有失败候选仍停在candidate。

## 生命周期与授权

`candidate → validated → enabled`；validated/enabled可以deprecated，任意未撤销版本可revoked。切换启用版本时旧head自动deprecated。回滚只能显式启用仍有效的deprecated版本，并重查原始来源和迁移证明；revoked不可复活。过期时间使用数据库时钟，代码指纹变化、来源任务/episode改变或验证工件缺失都会阻断使用。

版本revision与head revision共同做乐观并发检查；现有catalog事务锁序列化晋升/撤销与发布。迁移目标的修订、授权、父任务边界和证明工件持续检查。启用及回滚重放证据，执行时持续核查绑定/哈希/授权，当前调查证据另行重放。

Plan通过`skill={project_id, version_id, enabled}`显式选择，默认关闭。选用、换版本或项目纳入P1-08精确授权；project_id、memory、history、skill必须指向同一项目。全局或项目记忆停用同样阻断采用。Skill不能增加允许来源、工具、预算、执行阶段或发布权限。

固定流程收到计划建议，但不改原有工具顺序。单Agent、并行工具与有search/expand权限的调查分支可选择apply_skill，每执行至多一次；数据分支不能借此取得文档工具。复用当前已校验lookup种子，search和expand逐次计普通工具额度，Journal保留真实操作并支持已完成步骤回放。组合动作不隐去工具计费。准备时的证明核查属于额外本地开销，不能因无provider请求就称为零成本。

生成前、Journal步骤前后、正式发布前复核当前Skill。执行中撤销可保留在途结果，但阻断正式发布。报告保存skill_usage及采用记录；以后读取附动态skill_audit，显示旧采用版本当前是否仍有效。审计不会改写原不可变报告。

## 持久数据与接口

0009迁移增加四张表：memory_procedure_versions、heads、validations、adoptions。定义和出处保留哈希；验证证明和来源正文使用已有内容寻址工件。生命周期事件复用记忆审计日志，无新运行依赖。

| 接口 | 作用 |
| --- | --- |
| POST /procedures/candidates | 从episode创建候选 |
| GET /procedures/{version_id} | 版本与当前head |
| POST /procedures/{version_id}/validate | 有界迁移验证 |
| POST /procedures/{version_id}/state | 启用、停用或撤销 |
| POST /procedures/{version_id}/rollback | 重查后显式回滚 |
| GET /procedure-validations/{validation_id} | 查看验证状态与证明指针 |
| POST /procedure-validations/{validation_id}/abort | 显式终止running验证 |

全部沿用认证调用者身份，JSON不接受owner_id，也不接受任意原文路径。开发演示使用独立临时schema，清理后保留本地哈希工件；不能拿已清理的演示ID直接调用个人开发库。

## 验收解释

这里提取的是“成功完成且可回放的范围检查过程”，不是“已证明语义正确的调查答案”。最终不足的任务可以提供这种程序性经验。应在未来综合实验中用独立审查判断答案质量，不将COMPLETED标签替代成功标准。

普通文字经验与Skill的工程比较采用同来源/范围/资源上限，分别执行脚本决策。动作数差异由脚本设定，只证明接口与额度接线，不能证明模型更聪明、更省费用、更快或已经学会通用流程。P1-16的真实公平比较、独立人工评价和失败归因目标完整保留。
