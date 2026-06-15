# 采购单生成 — 赛狐接口契约 & 映射（2026-06-15 攻破 apifox 文档拿到）

## 赛狐采购单创建接口
`POST /api/purchase/create.json`（赛狐 OpenAPI，经 sellfox_client.call）

| 字段 | 必填 | 说明 / 取值 |
|---|---|---|
| warehouseId | ✅ | 仓库Id。采购计划 warehouseName="默认仓库"→**56115**；品牌仓见下表 |
| action | ✅ | 0=保存草稿 / 1=提交 / 2=提交并下单 |
| includeTax | ✅ | 1含税 / 0不含税（采购成本是不含税¥→默认 0） |
| supplierId | — | 供应商id，按品牌：链条系→**21797**，其他→**539122** |
| customPurchaseNo | — | 自定义采购单号 |
| remark / shipFee / otherFee / paymentMethodId / partyaId / purchaserId / isExpediting / currency / exchangeRate / createTime | — | 可选 |
| items[] | ✅ | 采购明细数组（每个采购计划明细一项）|

**items[] 字段**：
| 字段 | 必填 | 来源（采购计划明细）|
|---|---|---|
| num | ✅ | 采购量 = planNum |
| sku | — | sku |
| commodityId | — | commodityId |
| perPurchase | — | 采购单价¥ = purchaseCost（最多4位小数）|
| purchasePlanNo | — | 关联采购计划号 = plan_group_no（多个逗号分隔）|
| expectArrivalTime | — | yyyy-MM-dd |
| priceIncludeTax / taxRate / priceAndTax / taxAmount | — | 含税相关 |
| fnSku / shopId / exclusiveTypeStr | — | 仅专属库存用（一般不传）|

## ID 映射

**供应商（赛狐 /api/supplier/pageList.json 实测，仅3家）**：
- `21797` 杭州朗格链条有限公司 ← Zentop / Byane / RazEdg（链条系）
- `539122` 安庆市嘉欣医疗用品科技有限公司 ← 其他（Serenorch 等默认）
- `21796` 杭州保力五金工具有限公司（暂未用）

**仓库（/api/warehouseManage/warehouseList.json）**：
- 56115 默认仓库（采购计划用这个）/ 56322 Zentop-北美仓 / 57242 BYANE-北美仓 / 136137 RazEdg-北美仓 / 101372 Serenorch-北美仓 …

**站点**：链条系（zentop/byane/razedg）默认**美国**；店铺从 SKU 前缀→品牌→已绑定店铺主体推。

## 采购计划状态（明细行 status + reviewTime 反推）
- status=3 / reviewTime空 → **待审核**；已审批无PO → 待采购；已审批有PO → 已采购

## 采购全流程接口（本系统驱动赛狐状态，用户要执行一遍）
状态链：**待审核**(采购计划status=3) →[工厂确认,本地核对]→ **待采购** →[生成采购单 create]→ **待下单** →[下单 order]→ **待到货** →[到货 arrival]→ **已完成**

- 生成采购单：`POST /api/purchase/create.json`（上表，action 0草稿/1提交/2提交并下单）
- **下单** `POST /api/purchase/order.json`：`{"purchaseNos": ["采购单号"]}`（仅此一字段）
- **到货** `POST /api/purchase/arrival.json`：`{"purchaseNo":"采购单号", "items":[{"sku":必填, "goods":良品数必填, "defective":次品数必填(无填0), "fnSku"?, "shopName"?, "arrivalRemark"?}], "arrivalType":0正常/1快捷}`
- **采购单实时状态查询** `POST /api/purchase/page.json`：`{"purchaseNos":["采购单号"],"pageNo":1,"pageSize":1}` → data.rows[0].status：**-3草稿/-1待审核/0待下单/1待到货/2已完成/3已取消**

**⚠ 单号字段名两接口不一致（2026-06-15实测实建 PO2606150006 确认）**：
- create.json 返回 data = **list[{`purchaseOrderNo`, id, ...}]**（单号字段=`purchaseOrderNo`，不是 purchaseNo！）
- page.json 返回 data.rows[].**`purchaseNo`**（=同一个 PO 号）；page 的入参 `purchaseNos` 用 PO 号字符串可查到
- **action=1（提交）生成后采购单 status=0=待下单**（实测，无中间审批环节，符合状态链）
- 入库列 `purchase_plan_confirms.purchase_no` 已扩到 VARCHAR(255)（原 64，曾因把整个返回对象误塞进去 1406 报错）

- **作废** `POST /api/purchase/cancel.json`：`{"purchaseNo":"采购单号"}`（单值；已完成status=2的单赛狐多半拒绝）
- page.json 入参：pageNo/pageSize/purchaseNos/warehouseIdList/status/createTimeStart/createTimeEnd，**无 purchasePlanNo 过滤**。status 全集：-1待审核/-2已驳回/-3草稿/0待下单/1待到货/2已完成/3已取消。

**⚠⚠ 采购计划(PPG)状态赛狐 OpenAPI 改不了（2026-06-15 地毯式确认）**：402个接口全扫，purchasePlan 模块**只有 create/search**，全系统无任何审批/工作流接口。采购计划明细带 processInstanceId/canApproval → 审批走赛狐**内部工作流引擎**，未开放 OpenAPI。生成采购单**不改变采购计划状态**（实测 PO2606150006 status=2已完成，但 PPG2606150001 仍 status=3待审核；PO 侧 purchasePlanNoList=['PPG2606150001'] 关联正确，计划侧 purchaseNo=None 单向）。**结论**：采购计划审批只能采购员在赛狐界面做(两边并行)，或逆向赛狐商家后台内部接口(需后台登录态/抓包)。本系统驱动的赛狐状态=采购单PO全流程。

**防重复建单**：page 无 purchasePlanNo 过滤 → 防重靠拉近45天采购单列表逐个匹配 `purchasePlanNoList`(_existing_po_for_plan)。create 前：本地已记 purchase_no 或赛狐已有未取消关联单 → 拒绝(除非 force=True 强制生成)。作废=逐个 cancel.json，全成功则本地回退「待采购」可重生成；部分失败(如已完成单)保留。

业务：本系统做 工厂确认(本地)+生成采购单+下单+到货+作废 全套，每步(除工厂确认)调赛狐改**采购单**状态。生成采购单后 get_confirm 查 page 拿采购单实时状态展示。和赛狐采购员**两边并行**。

## apifox 文档抓取方法（以后拿其它赛狐接口入参复用）
文档 https://sellfoxapi.apifox.cn （密码保护 PASSWORD_PROTECTED），projectId=1827046, branchId=3160848：
1. `POST https://api.apifox.cn/api/v1/published-projects-auth` form: `id=1827046&password=<密码>` → set cookie `apiDocToken.1827046`
2. 带 cookie + header `X-Project-Id/X-Branch-Id`：
   - 接口详情：`GET /api/v1/published-projects/1827046/http-apis/{apiId}?branchId=3160848`
   - 入参 definitions：`GET /api/v1/published-projects/1827046/data-schemas?branchId=3160848`（requestBody.jsonSchema 的 $ref → #/definitions/{id} 在此展开）
   - 接口树：`.../published-projects/1827046/http-api-tree`
脚本：C:\Users\zane\AppData\Local\Temp\claude\apifox_final4.py + apifox_schema2.py
