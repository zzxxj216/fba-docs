---
name: doc-generate
description: FBA 发货文件生成（托书/报关资料/投保单/采购合同/9810/装箱单）：校验→修数据→按模板生成→归档→打包。触发词：生成文件、托书、报关资料、投保单、文件生成、打包、校验。
---

# 文件生成 skill（校验 → 生成 → 归档）

流程：**定位批次 → 校验 → 修数据(经确认) → 确认模板与参数 → 生成 → 核对 → zip 交付**。
一律走中转站 `http://127.0.0.1:8000`。

## 红线（绝对禁止）

- `DELETE /api/docs/{id}`（删文件+记录）、`DELETE /api/batches/{id}`、
  `DELETE /api/{resource}/{id}`（模板/产品/品牌…）——需要清理只报告等用户。
- **价格/日期系数不许手算硬编码**（×1.13/×1.1/扣重 0.5/汇率 7 等全在 RuleConfig）：
  对话里不复述具体数值当规则，改系数走 `PUT /api/rule-configs/{id}` 且须用户确认。

## 关键语义（判读响应必须知道）

- **校验是强制前置**：generate 内部先跑校验，不过时返回 **HTTP 200 + `{blocked:true, report}`**
  ——不生成任何文件。判成败看响应体（blocked/errors 键），不能看状态码。
- **逐模板逐货件独立报错**：`{generated:[...], errors:[str], notes:[str]}`，部分失败不回滚；
  修复后**只重传失败的 template_id** 再 generate。
- **覆盖语义**：同路径文件静默覆盖（无备份无版本），且 GeneratedDoc 每次新增记录不清旧
  ——docs 列表和 zip 会累积重复/陈旧条目。用户手工改过 output 里的文件时，重生成会冲掉
  手工改动，**生成前必须问**。
- 改 base_date/contract_date 会换输出目录/文件名（`output/{批次名}-{MMDD}/`），旧产物残留，
  提醒用户旧文件的存在（清理由用户手动）。

## 动线

### 1. 定位与全貌
`GET /api/batches` 找批次 → `GET /api/batches/{id}/full`。重点看：base_date/contract_date
（决定目录名/文件名/合同号日期）、每货件 forwarder_id/forwarder_order_no、docs（已生成过
什么，避免误报"首次生成"）、suggested_template_ids、sop 进度。

### 2. 校验
```
GET /api/batches/{id}/validate    →  {passed, errors:[{level, scope, field, msg, fix_hint}]}
```
- passed=false → 停下，按 scope 分组汇报。level=error 阻断必须修；**warn 不阻断但要念给
  用户拍板**（缺申报要素→报关资料该栏空着出；自动建档产品→品名/HS 可能是自动填的错值）。
- fix_hint 翻译成动作：`products:{id}`→PUT /api/products/{id} 补报关字段/箱重；
  `products:0`→需新建产品档案；`shipments:{id}`→PUT /api/shipments/{id}；
  `companies:{id}`→PUT /api/companies/{id} 补英文抬头；`batches:{id}`→PUT /api/batches/{id}
  绑品牌/主体/工厂。

### 3. 修数据（逐条经用户确认，不猜值）
货件/明细一律走 `PUT /api/shipments/{id}` / `PUT /api/shipment-items/{id}`（改动自动进
edited_fields，赛狐重同步不会冲掉），不绕过接口。改完回第 2 步重校验直到 passed=true。

### 4. 确认生成参数（停·等用户拍板）
1. 模板集：以 suggested_template_ids 为底，逐个报"模板名 + doc_type + granularity"让用户
   增删。**永远显式传 template_ids，不传空数组**（品牌没配默认模板集时空数组=静默空转，
   看似成功实际什么都没生成）。
2. base_date/contract_date 现值确认；要改先 `PUT /api/batches/{id}`（并提醒目录名会变）。
3. 投保单等 requires_forwarder_no 模板：检查各货件 forwarder_order_no，缺的明确告知
   "会置空生成 + notes 提示，回填单号后需重生成"——**置空的投保单发出去是事故，拦一道**。
4. docs 里已有同模板记录 → 告知会覆盖磁盘同名文件并新增一条记录。

### 5. 可选干跑
拿不准的模板：`GET /api/batches/{id}/preview?template_id=N`，把 groups[].filename 和关键
cells 给用户核对。注意 preview 对缺货代单号显示 skipped，但实际 generate 是置空生成——
以 generate 的 notes 为准。

### 6. 生成
```
POST /api/batches/{id}/generate   {"template_ids": [...]}
```
三分支：`blocked:true`→回第 2 步（数据刚被改坏）；否则逐条汇报 generated
（doc_id/filename/货件）+ errors（哪个模板哪个货件、什么原因）+ notes（置空生成提示）。

### 7. 核对与交付
- 关键文档抽查：`GET /api/docs/{doc_id}/preview`（编号/日期/金额格），或报路径
  `output/{批次名-MMDD}/{doc_type}/` 让用户打开看。
- 打包：`GET /api/batches/{id}/zip`（保留目录结构；重复记录会产生重复条目，交付前说明）。
- SOP：发送类步骤（send_factory/send_forwarder：托书→报关→投保顺序）由用户完成后
  `POST /api/batches/{id}/sop {"step_key": "...", "done": true}` 勾进度。

## 与其它 skill 的衔接

- 上游：建仓 skill（货件/FC 齐了才有生成对象；箱唛标签走建仓 skill 门 C，同一 output 目录）。
- 询价 skill 选定货代后需落到货件（PUT /api/shipments/{id} 设 forwarder_id）——托书模板
  按货代匹配靠它；投保单依赖人工回填的 forwarder_order_no。
- 两级采购合同（朗格系 BY/ZE/RA：工厂→店铺、店铺→星盟，系数走 RuleConfig）：
  `POST /api/batches/{id}/contracts`，body `{persist: true}`（persist=false 试跑：只写文件
  不落记录）。返回 {generated:[路径], warnings}。warnings 里"缺赛狐全名/成本"要念给用户；
  同名文件静默覆盖，生成前与其它文档同样确认。其它店铺调用会生成单份合同，同端点。

## 待拍板提示（doc_rules 里的 ⚠️ 项，首次生成对应文档时主动提醒）

托书 E3 各主体客户编号 / 报关资料 SKU>12 块怎么拆份 / 投保系数舟峰保峰 1.65 vs 1.695 /
报关境外买方各店名址——遇到相关店铺不要默认某个口径，先问用户。
