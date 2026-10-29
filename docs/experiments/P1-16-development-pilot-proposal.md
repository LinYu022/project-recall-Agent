# P1-16当前实现开发基线：首批小试方案

本方案推进独立开发基线的准备，不改变已结束的270格实验、旧评分、人工原判或生成实现。旧测试资料已见，不再标为未见；本批只用早期开发来源。四种主方法、消融、记忆实验及完整项目目标均保留。

## 精确范围与批准状态

新开发计划ID：`17a29b9475174de0b5277f6e8259fcb1353e25c57cce314662378380a224dde4`。

[可审查计划](../../evals/development-baseline-v0.1/prepared-plan.json)固定当前生成器全部代码指纹、运行器、资料发布、8个任务、参考定位与额度。固定流程是原有默认方法；本批不按旧测试赢家选择方法，不宣称已经完成四方法开发比较。

2026-10-29用户已批准本批资料与最多10次请求，原话为“批准本批，且可多于三次请求，但不高于十次”。本计划仍执行更小的 **3次** 程序上限，分别供前三个任务各1次，失败/未知也占额度，不自动重试。发送到`https://api.deepseek.com/chat/completions`，模型`deepseek-flash`，每次输出上限2000 tokens。实际费用以账户账单为准，当前不虚构金额。

新批准的精确原话、问题身份与本计划绑定保存在[批准记录](P1-16-development-pilot-approval.json)；其中人类授权上限10、当前实际执行上限3分别记账。旧P1-09及原P1-16最多584次批准不移用。命令参数或文件字段本身不构成人类授权，维护者在收到上述实际回复后才登记本机批准文件并启动真实模式。

## 资料与发送内容

| 来源 | 官方网址 | 用途 |
| --- | --- | --- |
| Prius召回10V039000 | https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=10V039000 | 车型、ABS条件、后果与补救 |
| Prius调查PE10006结案 | https://static.nhtsa.gov/odi/inv/2010/INCLA-PE10006-1281.pdf | 结案依据、继续监测与推断边界 |
| Ford召回17V331000 | https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=17V331000 | 门把盖附件/侧碰条件 |
| Ford召回17V652000 | https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=17V652000 | 拉索弯折、进水结冰及补救 |

只发送系统指令、表内任务问题/范围，以及普通固定流程从这些允许来源得到并最小化的公开文本/查询/关系结果。发布内虽有其他资料，任务授权不允许将其用作本批证据。密钥仅用于请求认证，不进入模型输入/工件；不发送私人文件、完整投诉、VIN、历史答案、评分标签或人工反馈。号码按原生成器规则最小化，参考义务不进入生成侧。

## 8个完整分母

| 任务 | 检查内容 | 真实请求上限 |
| --- | --- | ---: |
| DEV2-PRIUS-RECALL | 适用范围、制动条件/表现、碰撞风险、免费补救 | 1 |
| DEV2-PRIUS-JOINT | 制动问题、结案、继续监测、所有混动车安全推断 | 1 |
| DEV2-FORD-PAIR | 两个车门缺陷的条件、风险、补救及关系边界 | 1 |
| DEV2-MISSING-YEAR | 必需年份缺失时澄清 | 0 |
| DEV2-INDIVIDUAL | 个车生产日期/配置等缺失时澄清 | 0 |
| DEV2-COMPLAINTS | 只给召回源时，不能计算投诉量或发生率 | 0 |
| DEV2-WRONG-YEAR | 允许冻结来源中的2011 Prius匹配与有限覆盖边界 | 0 |
| DEV2-HISTORICAL | 2010年2月1日已知信息无法由当前快照代替 | 0 |

来源是两个已见开发事件簇，重复1次。没有足够独立簇用于可信区间，也不能据此估计所有车型质量。将来更完整的独立开发基线和另一版未见测试仍需执行；这批只校准当前默认系统与评分，不能自动完成验收。

## 参考义务与人工审查

[义务定义](../../evals/development-baseline-v0.1/atoms.json)由实现代理从原始来源预先定位，16项定义在前三题中形成18项必需义务实例。引用定位保留源版本/单元/精确范围与哈希，同文的各车辆记录保留为替代证据，不按系统matched结果删gold。标签作者尚未独立核对，不冒充人类gold。

审查分别记录引用角色、整条结论、原子断言、必需义务、处置及整题；“严格满足全部义务”与人类定性的“核心任务完成”分开。未审/争议保持null，人工有效用时只实际计时或自报，不能用消息间隔代填。关系边界需对照两个原文，safety_boundary需核对特定范围与逻辑，单个引文存在不等于这些语义已自动成立。

审查包会隐藏运行元数据，但本批只运行固定流程；已读方案的审查者能够知道方法，因此不声称对其实现严格的方法盲法。独立人类语义判断仍与实现代理的参考标签分开。旧两个历史审查结果不能自动传给新答案作为评分。

真实本地工具预检已结束：8项工程预期全部满足，3个生成入口各保存1份输入并主动抛出`development_preview_stopped_before_provider`，另3项等待澄清/接受固定时间，2项给出不足或有限来源零匹配。未返回假答案，也没有模型质量结果。实际原状态3 failed、3 guarded、2 completed保持；这些程序状态不是独立语义通过标签。[机器核查](../verification/2026-10-29-P1-16-development-preparation.json)固定原始输入、逐格状态/耗时、来源/标签检查与481项工程验证；数据库已停止，新增provider请求0。批准后真实模式另行运行，不把预检记录改名为真实结果。

## 复现与恢复

```powershell
uv run --locked --offline python -m baselining prepare
$developmentPlan = '.local/P1-16/development-baseline/17a29b9475174de0b5277f6e8259fcb1353e25c57cce314662378380a224dde4.json'
uv run --locked --offline python -m baselining preview --plan $developmentPlan --bin-dir .local/postgres/runtime/pgsql/bin
# 下条仅在本计划获得实际批准、原话登记后才可运行；否则在模型/数据库初始化前拒绝。
uv run --locked --offline python -m baselining live --plan $developmentPlan --bin-dir .local/postgres/runtime/pgsql/bin
```

预检与真实目录隔离；全8格在运行前登记、先领取再执行。已开始/失败/未知格不会自动再发。每次启动使用独立本机数据库并正常停止，数据库文件保留审计；中断格保留原状态，不重建历史或猜测没有请求。各任务没有记忆依赖，新启动只能领取原not_started独立任务；保留先前数据库，不能将新数据库说成同一事务续跑。

真实账本固定为`.local/P1-16/development-baseline-provider-allowance.json`并绑定本计划，换输出目录不能重领额度。观察、模型输入/响应、用量与失败逐项保存。源码摘要依赖本机固定发布及工件，不代表已完成干净机器复现；公开打包与GitHub发布仍属P1-18。
