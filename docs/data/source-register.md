# 来源获取清单：P1-00 探索期

最新补充：首次小步后的人工探索已保存 16 个原始文件及其字节哈希，见 `docs/data/exploration-sources.json`；资料仅位于忽略目录 `.local/exploration/raw/`。本页以下保留首次查阅状态。该小型探索归档仍不是 P1-06 的 DatasetRelease，不代表正式历史可见性覆盖。

观察日期：2026-10-26（Asia/Shanghai）。本表是在线人工查阅记录，未归档 HTTP 原始响应，不构成原始快照 manifest、正式 Evidence 或历史可见性证明。P1-02 再实现原始字节保存、哈希、时间语义与版本。

| 来源 ID | 官方入口 | 当前用途与获取状态 |
| --- | --- | --- |
| NHTSA-DATA-PORTAL | https://www.nhtsa.gov/nhtsa-datasets-and-apis | 已在线读取；核对投诉、召回、调查下载及 API 文档入口，尚未下载批量数据 |
| NHTSA-API-CAMPAIGN-10V039000 | https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=10V039000 | 已通过网页读取工具读取 JSON 展示；用于 EXP-001 人工核查 |
| NHTSA-RECALLS-GUIDE | https://www.nhtsa.gov/recalls | 已在线读取；核对车型查询与个车查询的输出边界 |
| NHTSA-INVESTIGATION-RESOURCES | https://www.nhtsa.gov/resources-investigations-recalls | 已在线读取入口；具体调查文件的下载、解析和引用能力尚未核验 |

## EXP-001 字段观察

- API 展示的 `Count` 为 2。
- `results[0]`：`Make=TOYOTA`，`Model=PRIUS`，`ModelYear=2010`。
- `results[1]`：`Make=LEXUS`，`Model=HS`，`ModelYear=2010`。
- 两条记录的 `NHTSACampaignNumber` 均为 `10V039000`。
- 原始展示日期字段：`ReportReceivedDate="09/02/2010"`。本步未核验该接口日期格式，保持原值，不解释为某个 ISO 日期。
- 引用定位暂为“来源 URL + 结果字段路径”；未来正式引用还需绑定内容版本和原始文件。

## 访问问题与修订

最初尝试的 `recalls/recallsByCampaignNumber` 路径无法由网页工具读取，不能据此判断来源无记录。查阅官方 API 文档后改用 `recalls/campaignNumber`，成功读取。

当前命令环境直接访问 API 被网络策略阻止，未获得原始响应文件；在线人工核查由可用的网页读取工具完成。失败没有被记作“零召回”，也没有伪装成原始快照已下载。

以上仅证明本次工具读取所展示的字段，不证明未来接口响应不变，也不证明任意历史日期的内容版本。
