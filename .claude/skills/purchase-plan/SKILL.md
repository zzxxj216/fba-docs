---
name: purchase-plan
description: 赛狐采购计划处理（API线店铺 ZE/RA/BY 等）：按店铺/区域动态拉取采购计划，工厂确认，生成采购单（联动赛狐计划"已采购/已处理"），下单、到货直至"已完成"。触发词：采购计划、拉采购计划、采购确认、生成采购单、采购下单、到货登记。
---

# 采购计划处理 skill

按对话驱动赛狐采购线：**拉取 → 工厂确认 → 生成采购单 → 下单 → 到货**。
所有调用一律走本地中转站 `http://127.0.0.1:8000`（curl），**绝不直连赛狐 API**
（限流 1 次/秒、签名、防重都在服务端做好了）。

## 红线（绝对禁止，任何场景包括"清理测试残留"）

- `POST /api/purchase/cancel-order`（作废赛狐采购单）
- `DELETE /api/purchase/confirm/*`（删本地确认记录）
- `DELETE /api/batches/*`、`POST /api/inbound/plans/*/cancel`、一切 DELETE/cancel 类接口
- `create-order` 带 `force:true`（绕过防重会造重复采购单）
- 需要作废/删除/驳回时：**只报告，等用户在赛狐后台或前端手动操作**。

## 状态模型（先读懂再动手，汇报时不要混淆两套状态）

**赛狐采购计划状态**（明细级）：待审核(3) / 待采购(0) / 已采购(1，即用户说的"已处理") / 已驳回(4)。
- 赛狐 OpenAPI **没有**修改计划状态的接口（402 个接口已地毯式确认，purchasePlan 只有 create/search）。
- "已处理/已采购"**只能**通过真实生成采购单联动：create-order 时逐明细传 planNo(PP…)，
  赛狐自动把对应明细标记已采购。**不能不建单空标状态**——用户若要求"只标已处理不建单"，
  说明做不到，唯一途径是采购员在赛狐后台操作。
- 待审核→待采购 的审批也只能在赛狐后台做；遇待审核计划提示用户去赛狐审核。

**本地工作流状态**（PurchasePlanConfirm 表）：待审核 →[工厂确认]→ 待采购 →[生成采购单]→
待下单 →[下单]→ 待到货 →[到货]→ **已完成**。
- "已完成"是采购单(PO)/本地链的终态，不是赛狐计划的状态。
- 实时状态以 `GET /api/purchase/confirm/{pgn}` 返回的 `po_status_label`（赛狐 PO 真值）为准，
  赛狐采购员可能已在后台操作过同一计划。

## 前置检查

1. `curl -s http://127.0.0.1:8000/api/brands` 通 → 服务在。不通 → 后台启动：
   `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（工作目录 D:\amazon，
   **严禁加 --reload**，安全软件会卡死）。
2. 赛狐凭据异常会以 502 返回，detail 里有赛狐原始报错——数据问题和服务故障都走 502，
   必须看 detail 文本分类，不能只看状态码。

## 动线（对话 SOP）

### 1. 拉取（用户说"拉 ZE 美国的采购计划"之类）

```
GET /api/sync/purchase-plans?shop={店铺}&site={区域}&status={状态}&days={天数}
```
- `shop`：品牌缩写(ZE/RA/BY)或品牌名(Zentop/RazEdg/Byane)或店铺名；`site`：中文站名(美国)或
  国家码(US)；`status`：待审核/待采购/已采购/已驳回，用户没说就先不过滤，把 `status_counts`
  一起报给用户看全貌；`days`：默认 60。
- 呈现列表：组号(PPG…)/状态/计划总量/SKU 数/店铺-站点/创建时间/已导入(imported)/
  是否已建仓发货(has_shipping)。
- **检查 `fetched < sellfox_total`**：说明拉取被 500 条上限截断，提示用缩小 days 重拉，
  不要装作拉全了。
- 过滤后为空：报告实际存在的店铺名（从全量 plans 的 items[].shop_name 归纳），让用户确认写法。

### 2. 选定计划 → 展示明细 + 实时状态

用户点名某个 PPG 后：从列表数据展示逐 SKU 明细（sku/数量/箱数/采购成本/供应商），并
`GET /api/purchase/confirm/{plan_group_no}` 查本地状态 + 赛狐 PO 实时状态。
- 已驳回/待审核：停下，提示去赛狐后台处理（本 skill 无法审批）。
- 已有关联采购单（purchase_no 非空）：直接汇报现状，进入第 4/5 步而不是重复建单。

### 3. 门 1：工厂确认（纯本地，不写赛狐）

用户明示"已与工厂核对"后：
```
POST /api/purchase/confirm  {"plan_group_no": "...", "items": [{"sku","commodity_name","num","box_count"}...], "confirmed_by": "运营名"}
```
`confirmed_by` 填当前运营的名字（问用户或用已知身份）。本地状态 → 待采购。

### 4. 门 2：生成采购单（真实写赛狐，先预览后放行）

**第一步必须 dry_run 预览**：
```
POST /api/purchase/create-order  {"plan_group_no": "...", "dry_run": true}
```
向用户复述：仓库(warehouseId)/供应商(supplierId)/逐 SKU 数量与单价(perPurchase=不含税
采购成本)/计划明细号关联。**警告项**：供应商为空或仓库落默认 56115 → 品牌档案缺
sellfox_supplier_id/sellfox_warehouse_id，先让用户补档案再建单。
（参考：ZE/RA/BY=链条系，供应商 21797；仓 56322=Zentop-北美/57242=BYANE-北美/136137=RazEdg-北美，
详见 PURCHASE_ORDER_API.md，不要在对话里凭记忆报 ID。）

用户确认预览后再真建，`action` 按用户意图选：
- `"1"`=提交（建单后停在**待下单**，之后走第 5 步）
- `"2"`=提交并下单（直达**待到货**）——用户要"一步到位"时用
```
POST /api/purchase/create-order  {"plan_group_no": "...", "action": "2", "dry_run": false}
```
- `auto_arrival` 默认 **false**，除非用户明确说"直接登记到货"——它会按全部良品记到货，
  货没实际到仓会虚增赛狐库存，必须单独确认。
- 防重报错（"已有关联采购单 PO…"）→ 报告已有单号让用户决定，**绝不 force**。
- 成功后记下 `purchase_no`，并**回查联动**：重新
  `GET /api/sync/purchase-plans?shop=...`，确认该 PPG 的 status_label 变为"已采购"；
  没变则如实报告（明细号未关联成功，需人工在赛狐核对），不要谎称已处理。

### 5. 下单 / 到货

- 待下单 → 待到货：`POST /api/purchase/submit-order {"plan_group_no": "..."}`
- 待到货 → 已完成（**必须用户提供真实到货数据**）：
```
POST /api/purchase/arrival  {"plan_group_no": "...", "items": [{"sku","goods":良品数,"defective":次品数}...], "arrival_type": 0}
```
（arrival_type: 0=正常收货, 1=快捷收货。）

### 6. 收尾

汇报：采购单号 / 本地状态 / 赛狐 PO 状态(po_status_label) / 赛狐计划是否已联动"已采购"。
提示下一步可选：`POST /api/sync/purchase-plans/import-only {"plan_group_no"}` 落批次
→ 接建仓流程（见 SKILL_INBOUND.md 规划）。

## 失败处置

| 症状 | 处置 |
|---|---|
| 502 detail 含"限流"/40019 | 服务端已自动重试，仍失败则稍候重拉，不要连环快速重试 |
| 502 detail 含赛狐 token/40001 | 服务端已自动刷新，复现则报告用户查 .env 凭据 |
| 404 计划未找到 | 超 60 天窗口或组号打错；用 days 放大时间窗重试一次 |
| 防重拦截 | 报告已有单号，等用户决定（禁 force） |
| 到货失败(arrival_error) | 状态停在待到货，报告原始错误等用户处理 |

每次真实写操作（confirm/create-order/submit-order/arrival）成功或失败都在回复里如实记录
请求要点和结果，方便运营留档。
