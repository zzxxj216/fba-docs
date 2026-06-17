# 货代沟通智能体 设计 v2（建仓后 → 询价 → 归属 → 提取 → 比价 → 发文件）

> 现有 DESIGN.md 覆盖"建仓后文件准备（生成托书/报关/投保…）"。本文件设计**货代沟通自动化层**。
> **半自动**：智能体干起草/归属/提取/对比，关键决策（发询价、选货代、发文件）卡人工确认。
> v2 重写动因——实战暴露 4 个原设计没吃透的复杂点（见 §0）。阶段0 管道已真实验收（见 §1）。

## 〇、v2 要解决的四个真问题

1. **报全没报全（FC 覆盖）**：分仓后一个批次→多个目的仓 FC，头程要逐仓报；必须能校验货代"是否每个仓都报了、箱数体积对不对"。
2. **图片报价**：货代常发**价格表截图**而非文字，提取要上**视觉**（Claude vision）。
3. **多店铺共用一家货代 + 并发多轮 → 归属隔离**：同一货代/群里同时跑多个批次的询价，货代回复混在一个会话里，必须把每条回复**归属到正确的 Inquiry**，不串味。
4. **后续发文件**：选定货代后要发托书等给货代；货代也会回传提单/装箱单/账单，要提取归档。

## 一、渠道 qiweapi（阶段0 已验收，事实定稿）

- 入口 `POST http://manager.qiweapi.com/qiwe/api/qw/doApi`，Header `X-QIWEI-TOKEN`，body `{"method":"/x/y","params":{...}}`。封装在 `app/qiwe_client.py`（`.env` 读 `QIWE_TOKEN`/`QIWE_GUID`，未配优雅降级）。
- **发消息** `/msg/sendText`：`guid`+`toId`(外部联系人 told 或群 roomId)+`content`+`isNoNeedRead`；回 `isSendSuccess:1`+`msgServerId`。
- **群列表** `/room/getRoomList`（带 guid）→ roomId/roomName/群主/成员数。
- **收消息=Webhook**（无轮询）→ `POST /api/qiwe/callback`。真实结构：
  `{"data":[<事件>...],"code":0,"msg":"成功"}`；事件里 `cmd=15000` 才是聊天消息，正文 `msgData.content`，群 `fromRoomId`(0=私聊)，发送方 `senderId`，本实例账号 `userId`（`senderId==userId` 为自己回显，跳过），消息 id `msgUniqueIdentifier`/`msgServerId`，`timestamp` 秒，`msgData.reply` 为引用的原消息。系统事件（cmd 11001 登录态 / msgType 2001 已读回执）、订阅确认 `{"msg":"设置订阅成功!!"}` 都跳过。
- 风险/前提：①云端实例保持登录，异地登录有封号风险；②货代须是该实例外部联系人/群成员；③Webhook 需公网可达（开发用 gradio-tunneling，72h 过期；生产部署固定域名）。
- 待补接口（实现期查文档）：**图片消息下载/取媒体**、**发文件/发图片**、外部联系人列表。

## 二、领域模型（重构）

```
Brand 1─n Forwarder绑定   Batch 1─n Inquiry 1─n InquiryQuote 1─n QuoteLine
Forwarder 1─n ForwarderMessage（按 inquiry_id 分段；可后填/重指派）
```

- **Forwarder**（阶段0 已扩展）：`qiwe_external_userid`(told) / `qiwe_room_id` / `qiwe_guid` / `bind_brand_id`（**按品牌绑定**，一家可绑多品牌）/ `is_default` / `active`。
- **Inquiry 询价单**（一个批次一轮询价）：`batch_id`、`ref_code`（人读暗号，如 `INQ-HUH0619-1`，埋进正文便于归属）、`status`、`channel`(群/私聊)、`content`(定稿正文)、**`lanes_snapshot`**（JSON：分仓后目的仓清单 `[{fc, boxes, volume_cbm, weight_kg, skus}]`，**报全校验与对账的基准**）、`target_forwarder_ids`(多家比价)、`chosen_quote_id`、时间。
- **InquiryQuote 报价**（一家货代一份）：`inquiry_id`、`forwarder_id`、`source_type`(text|image)、`raw_message`/`raw_image_path`、`currency`、`channel`(渠道)、`valid_until`、`extract_confidence`、`is_chosen`。
- **QuoteLine 报价明细行（按 FC）**：`quote_id`、`fc`、`price`、`unit`(/kg、/票、/cbm)、`channel`、`eta_days`、`cutoff`(截关)、`remark`。一份报价可能逐仓多行或一口价一行。**覆盖校验** = `Inquiry.lanes 的 FC 集合 − Σ QuoteLine.fc`。
- **ForwarderMessage 消息流水**：`forwarder_id`、`direction`(in/out)、`msg_type`(text|image|file)、`content`、`media`(JSON：本地路径/cdn/尺寸)、`qiwe_msg_id`(幂等)、**`inquiry_id`(可空→归属后填)**、**`attribution_status`**(auto|manual|pending|none)、`reply_to_msg_id`(引用)、`raw`、`ts`。
- **ForwarderDoc 文件往来**（可并入 Message.media）：发给货代的托书 / 货代回传的提单·装箱单·账单；关联 batch/inquiry/quote + 抽取字段（提单号/船名航次/费用）。

> 相对阶段0 已建模型的增量：Inquiry 加 `ref_code`/`lanes_snapshot`/`channel`/`chosen_quote_id`；新增 **QuoteLine** 表；ForwarderMessage 加 `attribution_status`/`reply_to_msg_id`/媒体落地；新增 ForwarderDoc。

## 三、归属与隔离（多店铺共用货代/并发多轮——v2 核心）

**问题**：货代1 群同时承载 批次A、批次B 的询价；货代回复都在同一群，怎么归属到对的 Inquiry？

**三层策略（隔离做在"归属层"，不指望货代守规矩）**：

1. **发时埋点**：询价正文带 `ref_code` + 结构化标的（`【询价 INQ-HUH0619-1｜美国 3仓/209箱】`）。给后续归属留锚点。
2. **收时归属**（webhook 落 message 后跑）：
   - **引用优先**：payload `msgData.reply` 指向我方某条 out 消息 → 顺着它的 inquiry_id 归属（最强信号）。
   - 该货代**仅 1 个开放 Inquiry** → 直接归属。
   - **多个开放** → **Claude 归属**：把"该货代当前所有开放 Inquiry 的标的(FC/品名/箱数/ref_code)" + 这条回复喂 Claude 判断答的是哪个（回复通常带目的地/品名/箱数特征，能对上）。高置信→自动；低/模糊→`attribution_status=pending`，UI 标"待人工归属"，人点一下指派。
3. **兜底序列化（可选开关）**：对易混货代，限制"同群同一时间只开一个 Inquiry"，其余排队，发出时提示。

> ForwarderMessage.inquiry_id 后填、可重指派、可审计。提取/比价只认已归属消息。

## 四、图片报价提取（视觉）

1. webhook 收图片消息 → 取 `base64RawData` 或调 qiweapi 媒体下载接口拿原图 → 存本地 + `ForwarderMessage(msg_type=image, media=路径)`。
2. 提取：图片喂 **claude-opus-4-8（视觉）**，structured outputs → `QuoteLine[]`（逐 FC 价格/渠道/时效/截关）。**文本报价同一套 schema**，只是输入是文本。
3. 价格表多是多行多列 → Claude 直接读成 QuoteLine 数组；低置信行标黄让人核。

## 五、报全校验 + 对账（比价前的质量门）

`Inquiry.lanes`（分仓后所有目的仓，来自 placement / batch.shipments，权威）对照提取出的 QuoteLine：
- **缺仓**：lanes 的 FC ∉ 任何 QuoteLine → "货代漏报：FC XXX（箱数/体积）" → 一键追问。
- **多仓**：货代报了我们没有的 FC → 标异常。
- **数据对不上**：货代回的箱数/体积/重量 vs lanes 逐项 diff，标差异。

## 六、状态机

```
Inquiry: 待发送 →〔起草·确认〕→ 已发送 → 收集中
   （收集中滚动跑：归属 → 提取 → 报全校验/对账）
   → 比价待决（覆盖齐 + 多家到齐 or 超时）→〔人工选货代〕→ 已选货代
   → 发文件中 →〔生成·确认·发送〕→ 已发文件 → 完成
旁路：缺仓追问→回收集中；货代改价→更新/新增 Quote；归属失败→pending 待人工。
```

## 七、文件往来

- **发文件**：选定货代后复用 `excel_engine` 生成托书等 → 人工确认 → qiwe 发文件/图片给货代（`msg_type=file`，落 out 流水 + ForwarderDoc）。
- **货代回传**（提单/装箱确认/账单）：webhook 收文件 → 下载归档 → Claude 提关键字段 → 关联批次。

## 八、智能体职责（claude-opus-4-8）与人工确认点

| 智能体自动做 | 人工确认 |
|---|---|
| 起草询价正文（按 lanes + ref_code，措辞合适） | **发询价前**（看草稿、改、点发） |
| 回复**归属**到对的 Inquiry（引用/单开放/Claude 判别） | 仅"模糊"时人工指派 |
| **文本+图片**报价提取成 QuoteLine（价格/时效/截关/渠道） | — |
| **报全校验 + 对账**（lanes vs QuoteLine，标缺仓/差异） | 差异处置、是否追问 |
| 多家比价汇总 + 推荐选优（带理由） | **选哪家货代**（拍板） |
| 提取货代回传文件关键字段、归档 | — |
| 生成托书等（复用引擎） | **发文件前**（看、点发） |

## 九、与主流程/SOP 打通

建仓完成（有 FC/箱体积重）→ SOP「询价比价」步：起草→发→收集→比价→选→发文件（SOP 进度条加这几步）。批次详情加**「货代沟通」面板**：消息流水按 inquiry 分段 + 报价对比表（逐 FC）+ 报全校验 + 待人工归属/差异待办。

## 十、分阶段路线（修订）

- **阶段0 ✅ 管道**：发/收/匹配，已真实验收（货代1 群发→回"你好"→归属成功）。
- **阶段1 询价闭环 MVP**：品牌↔货代绑定 UI → 起草(Claude) → 群发多家 → 收回复**归属**(引用/单开放/Claude+人工兜底) → **文本+图片提取**成 QuoteLine → **报全校验** → 比价表人工选。
- **阶段2 对账 + 文件回传提取**：lanes diff 细化；货代回传文件归档抽字段。
- **阶段3 发文件**：生成托书→确认→qiwe 发货代。
- 贯穿：归属审计、消息留痕、状态机、视觉提取统一 schema。

## 十一、待确认（实现前）

1. **归属策略**：接受"Claude 自动归属 + 模糊转人工"为主？是否要强制 `ref_code` / 序列化兜底开关？
2. **图片样式**：货代价格表大致长啥样（逐仓表格 / 一口价 / 多渠道并列）？给 1–2 张真样，把提取 schema 定准。
3. **报全校验粒度**：到 FC 够，还是要"每 FC × 每渠道"？
4. **发文件**：发哪些（托书/报关/装箱单）？走 qiwe 发文件还是发图片？
5. **ANTHROPIC_API_KEY**：起草/提取/视觉/归属全靠它，何时配（阶段1 起即需要）。
6. 比价"到齐"判定：等齐 N 家 / 超时 X 小时进比价。
