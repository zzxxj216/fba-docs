---
name: forwarder-inquiry
description: 货代询价/比价/选定（企微渠道）：从已建仓批次起草询价、外发货代群、收集报价自动提取、整包比价、用户拍板选货代。触发词：询价、报价、比价、货代、选货代、催报价。
---

# 货代询价 skill（企微 qiweapi 渠道）

流程：**起草 → [外发闸门] 发送 → 收集(自动) → 比价汇报 → [拍板] 选定**。
一律走中转站 `http://127.0.0.1:8000`。货代沟通走企微，与飞书无关。

## 红线与安全边界

- 无取消权限：取消询价没有 REST 端点（只有飞书卡片动作），**skill 不做取消**，用户不要了
  就停手报告现状。`DELETE /api/forwarders/*` 等一切删除类禁调。
- **系统永不向货代确认发货、承诺货量、议价、谈付款**——skill 只能外发两类内容：
  询价正文、缺仓追问。任何其它话术不代发。
- 询价正文**不带内部编号/暗号**（2026-07-07 用户拍板）：服务端草稿已按固定模板生成
  （"您好，这几个仓麻烦报一下价格：FC：N箱/方/kg…"），skill 不得往正文添加 ref_code
  或报价要素。注意：`/followup` 的追问草稿默认带 ref_code——发送前提醒用户删掉编号。

## 前置检查

1. `GET /api/qiwe/status` → configured=true；false 则停（QIWE_TOKEN 未配，发不出去）。
2. 批次已建仓分仓（货件有 FC）——没有 lane 时创建询价会 400"请先完成建仓/分仓"。
3. **查重**：该批次是否已有开放询价（status ∈ 待发送/收集中）。有 → 续用它（GET
   /api/inquiries/{id}），不重复新建。另注意：REST 线发送后批次 status 不变"询价中"，
   飞书整包流按 status=已建仓 筛选可能对同批次再询一遍——发送前把这个双轨差异告知用户。

## 动线

### 1. 起草
```
POST /api/inquiries   {"batch_id": N}
```
向用户展示：正文 content 原文、lanes（逐仓 FC/箱/方/kg，与建仓结果核对）、目标货代名单
（target_forwarder_ids 对应的货代名）。核对点：正文无编号；目标货代不全 → 品牌没绑货代，
让用户在 `/api/forwarders` 补 `bind_brand_id` 后重建询价。

### 2. 外发闸门（停·必须用户明示"发"）
明确说"下一步是真实企微消息发给这些货代群"。用户要改措辞 →
`PUT /api/inquiries/{id}/content {"content": "..."}` 改后再展示。

### 3. 发送（一次性动作，严禁重复）
```
POST /api/inquiries/{id}/send
```
逐条汇报 results（哪家成功/失败及原因）。成功后 status=收集中。
**没有重复发送防护**：再调一次会原样重发骚扰货代——发送前查 status，收集中=已发过；
个别失败的修复货代配置后由用户决定补发方式，不自动重发全员。

### 4. 收集期（报价自动进来，skill 只轮询两处）
报价经企微 webhook 自动落库→自动归属→LLM 自动提取→缺仓自动催一次，无需干预。轮询点：
- `GET /api/inquiries/{id}/comparison`：看各货代是否报全（complete/missing）。
- `GET /api/inquiries/messages/pending`：自动归属失败的消息队列。有 → 贴出正文+货代名
  让用户指认，确认后 `POST /api/inquiries/messages/{msg_id}/assign {"inquiry_id": N}`，
  **assign 后必须补 `POST /api/inquiries/{id}/extract-text {"forwarder_id": N, "text": 消息原文}`**
  （人工指派不自动提取，漏了比价表就看不到这家）。
- 长时间没回：无定时催价机制。`POST /api/inquiries/{id}/followup {"quote_id": N}` 拿追问
  草稿（含缺仓清单），**给用户看并提醒删 ref_code**，用户确认后才发送。
- 收不到任何回复先怀疑 webhook 隧道挂了（72h 过期），报告用户检查，别怪货代没回。

### 5. 比价汇报
`GET /api/inquiries/{id}/comparison`，表格呈现：行=货代；列=整包总价(币种)/运费小计/
月度报关费/是否报全(缺哪些 FC)/风险/置信度 + 逐仓明细（FC/单价/单位/渠道/时效/截关）。
- 口径说明必须讲：整包总价=Σ各仓 max(单价×计费量, 最低消费)+**整月一笔**的报关费；
  混币种(CNY/USD)系统不折算，提醒用户确认口径。
- confidence < 0.7 的报价：贴原文让用户核对，LLM/正则提取可能有错。
- 末尾给 recommended_quote_id 和理由，并明说"这只是推荐，选哪家你拍板"。

### 6. 拍板选定（停·用户点名后）
```
POST /api/inquiries/{id}/choose   {"quote_id": N}
```
汇报后**明确边界**：仅系统内记录（status=已选货代），没有给货代发任何确认消息；
不触发亚马逊分仓/运输确认；不写货件的 forwarder_id/货代单号。

### 7. 衔接（只提示不代做）
- 把选定货代落到货件（生成托书等文档需要）：`PUT /api/shipments/{id} {"forwarder_id": N}`
  ——逐货件、经用户确认后代改（自动进 edited_fields 保护）。
- 亚马逊确认分仓+运输 = 建仓 skill 的门 B（confirm-placement），由用户在那条线上确认。
- 货代单号 forwarder_order_no（投保前置）等货代给单后人工回填：
  `PUT /api/shipments/{id} {"forwarder_order_no": "..."}`。

## 失败处置

| 症状 | 处置 |
|---|---|
| 400 批次没有可询价的目的仓 | 先走建仓 skill 完成分仓 |
| send results 里某家 error"未配企微联系方式" | 让用户在 /api/forwarders 补 qiwe_room_id（可从 GET /api/qiwe/rooms 选） |
| 报价迟迟不到 | 查 messages/pending；查 webhook 隧道；人工跟进，不自动重发 |
| 提取结果明显错（多仓同价/缺行） | 贴原文，用 POST /extract-text 重提或让用户口述后人工录入 |
| 同群多品牌货代归属串了 | pending 队列人工指认，assign+extract-text |
