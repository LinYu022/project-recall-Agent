# P1-03 第二小步：证据条件与五类关联

日期：2026-10-26。本小步完成；本记录描述第二小步时的状态。后续查询 API 与模块验收已在 `docs/learning/P1-03-step3.md` 完成，等待理解确认。练习附参考答案。

## 目标、原理与依赖

把“车型相同”细分为可解释的范围、症状、提及、排除与未知关系。依赖 P1-02 的原字节、版本及位置，和 P1-03 首步的保守标准化；结果供后续查询 API、范围过滤和调查引用使用。

本步关键原理有三点：已知真/已知假/未知不能混成布尔值；确认关系类型不等于确认因果；引用能回放不代表引用一定语义支持更强结论。详细规则见 `docs/data/entity-link-rules-v0.1.md`。

## A：先做带版本的条件事实

`entities/scopes.py` 定义 RecordLocator、ProductionRange、FactRule、ScopeRules。请求只给位置；规范事实来自固定规则文件，而不是用户输入一段自称官方结论的话。

```python
if fact.source_version_id != version["version_id"]:
    continue
```

即使新响应沿用相同 URL、车型和一句文字，旧版本规则也不自动用于新记录。命中的事实还需验证摘录在指定字段中唯一出现，再保存原文字符偏移和字段哈希。不存在、重复、来源字节被改写等情况均失败，不能默认为成功空结果。

真实来源 14V284000 的生产范围是 2006-08-18～2010-09-11；它不是一辆 Escape 的实际制造日。20V332000 的 3.5L Ecoboost 是召回范围条件，也不能填成某条 2016 F-150 投诉的发动机配置。

新 entity-rules/0.2 嵌入 scope-rules/0.1，仅用于本步的范围对象。旧计数、旧数据库投影继续使用 0.1。新增事实不是改写 SourceRecord，也不会令旧实验记录改变。

## B：三值判断与有限确认

`entities/links.py` 先比较车辆三元组，再检查双方已有的生产范围和同键互斥配置。日期区间相交采用包含两端的条件：

```python
max(left_start, right_start) <= min(left_end, right_end)
```

只有一边有生产范围时，不能代入无穷区间或车型年；只列入 unknown_conditions。已经有确定的车型或年份不符时可排除，不必等待所有缺口补齐。

症状关系需要双方各自的审查标签和摘录；相同部件本身不够。官方提及是有向关系：22V150000 提及 20V332000，不能反向断言旧召回提及新召回。原文的“扩展”比通用“显式提及”更强，本层只确认后者，完整扩展结论仍需后续语义校验。

工件固定五种判断、双方版本、适用条件、未知条件、规则指纹与支持证据。confirmed 表示该关系类型成立；已记录维度重叠可与完整适用性未知同时成立。所有关系均不证明因果。

## C：真实反例与参考答案

重新从 NHTSA 官方 API 获取 5 份响应，共 33 行；14V284000 和 20V332000 与探索旧文件的 body 哈希相同。来源及准确取得时间保存在 `docs/data/scope-source-observations-2026-10-26.json`，没有伪造早年获取记录。本步未把这些新快照导入 PostgreSQL，真实数据库回归使用原有四份快照。

17V331000 的 `/results/2` 与 17V652000 的 `/results/1` 均为 **2016 FORD F-150**，部件都为 `LATCHES/LOCKS/LINKAGES:DOORS:LATCH`。前者涉及原车黑色/车身同色门把手及后续经销商镀铬罩配件，可能在侧碰时解锁；后者涉及拉索弯折或进水结冰，可能出现行驶中车门打开。

参考答案：两者已记录的车型年范围重叠；审查后的 DOOR_UNLATCHING 是宽泛症状相似。不得说“同一缺陷”“该投诉已经归因于其中某次召回”或“某车必然适用”。生产日期、配件、个车范围成员等条件仍未确认。

第二个真实例子：22V150000 的 `/results/0/Remedy` 写明 `This recall is an expansion of NHTSA recall number 20V-332.`。程序保留 20V-332，并按明确审查的编号格式对应 20V332000。这是显式提及的真实材料验证，不是用合成句冒充官方文件。

第三个例子：14V284000 的首行是 2008 Escape，与 20V332000 的 2016 F-150 行车型/年份不符，确认 excluded_by_scope。排除的是这对记录的范围对应，不是断言任何一辆车没有其他缺陷。

参考结论由实现代理对照原文编写，没有取得用户逐项独立标注，不能称为人工金标准。所有已看过的事件、扩展和改写都排除于未来未见测试；分簇建议保存于参考清单。

## 动手范例、成功与失败验证

本机已保留固定快照，可直接运行：

```powershell
uv run --locked --offline python -m entities link examples/link-door-recalls.json
uv run --locked --offline python -m entities link examples/link-official-reference.json
uv run --locked --offline python -m scripts.verify_entity_links
```

第一条参考输出中，scope_overlap、symptom_similar、applicability_unknown 为 confirmed；后者 stage=unknown。officially_referenced、excluded_by_scope 为 candidate。结果保存到响应 artifact 路径；用编辑器检查 supporting_evidence、unknown_conditions、原文位置和 causation=not_established。

第二条参考输出中 officially_referenced=confirmed；不表示“新旧召回中每辆车都相同”。第三条应得到 facts=7、cases=3；重复运行引用工件应不变。CLI 的 assessed/退出 0 仅表示评估执行成功，不表示所有关系或适用性都确认。

以下练习直接给参考答案，合成用例已经实现，无需破坏真实快照：

| 动手场景 | 参考答案与验证点 |
| --- | --- |
| 两生产范围分别止于和始于 2010-09-11 | 当天相交，不按日期排除；不代表全部配置已匹配 |
| 将第二范围起点改为 2010-09-12 | 已知日期不相交，确认 excluded_by_scope |
| 一边 3.5L ECOBOOST，另一边缺发动机条件 | configuration:engine 未知，不当作相等或冲突 |
| 双方分别明确为同一互斥值域中的 3.5L ECOBOOST 和 5.0L V8 | 配置冲突，可排除；必须是已审查的互斥字段 |
| F150 对 F-150 | 仍只有候选，scope_overlap 不确认 |
| 只有同部件，没有双方症状摘录/标签 | symptom_similar 保持 candidate，不晋升 |
| 显式提及出现在否定句 | 仅确认“提及”，不能说认可、适用或因果成立 |
| 摘录不存在/出现两次、原字节篡改、位置越界 | 操作失败，CLI 返回 2，不生成肯定结论 |
| 相同规则名修改内容后保存到同一工件库 | 拒绝；应使用新版本名称并保留旧工件 |

```powershell
uv run --locked --offline python -m pytest -q tests/test_entity_links.py --basetemp .local/practice-links
```

参考结果：24 项通过。测试使用 SYNTHETIC 标记的可控来源，覆盖五类关系、原文/版本验证和失败边界，不需要外部服务。额外防止把投诉提及升级为官方声明，或将未绑定车辆位置的条件套用于多个产品。

## 验证记录、设计取舍与剩余工作

本轮累计 133 项无数据库测试、32 项真实 PostgreSQL 测试通过，合计 165 项；另有 7 类真实回环 HTTP 检查。Ruff 和选定 24 个源码文件的 mypy 通过。真实归档 JSON 回放独立完成 7 条事实摘录与 3 组参考判断。临时 PostgreSQL schema 清理、数据库和 HTTP 服务已关闭。无新增依赖、无真实模型实验、无 GitHub 远端 CI。

初次新增 CLI 的类型检查发现分支变量的推断类型不兼容；为结果声明字典类型并区分两类请求变量后通过。不是源资料获取失败，也未将失败算作零条匹配。记录见 `docs/verification/2026-10-26-entity-links-{real,offline,postgres}.json`。

本步选择可审查的有限规则，而非未经验证的通用自由文本抽取；这是实现策略，不是删减生产日期、配置或其余模块目标。所有未覆盖字段继续显示未知。新关系结果使用不可覆盖文件工件，未新增数据库迁移；API、正式发布与通用文档解析分别按后续小步和 P1-06/P1-04 完成。

对应实现提交：`bdcce52`。无原始投诉、临时数据库、关系工件、环境或密钥进入本次提交。

下一小步仍在 P1-03：接通真实结构化查询与关联查询 API，完善请求的快照/时间边界、错误映射及模块整体答案核验。模块验收后等待理解确认，再进入 P1-04。
