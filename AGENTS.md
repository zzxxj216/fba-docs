# AGENTS.md — Codex/Claude 驱动指南（运营端）

你（AI agent）通过本机 API `http://127.0.0.1:8000` 驱动 FBA 发货流程。服务未起时先启动：
`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（**严禁 --reload**）。
建仓执行走 mcapi：`{MCAPI_BASE}/api/v1/sellfox/inbound/*`，请求带 `X-API-Key: {MCAPI_KEY}`。

## 交互模型：分段一次确认，段内连贯执行

- 每个业务段开始前，**一次性**向用户复述：将执行的动作清单 + 关键参数 + 全部警告项。
  用户确认后整段连贯跑完，**中途不再逐步询问**；段尾汇报结果（成功/失败/单号/产物）。
- 只有四类情况才在段中停下：① 固定拍板点（选分仓方案 / 选货代——这是业务决策不是确认）；
  ② 事故级风险（重生成会覆盖用户手工改过的文件、auto_arrival 虚增库存）；
  ③ 占坑 409 / 防重拦截 / 报错含"草稿计划待手动清理"——停下报告，绝不重试绕过；
  ④ 实际情况与开段时复述的计划出现偏差。
- 修数据不逐条问：把要改的字段一次列全，确认后批量执行。含糊值不猜，列出来问一次。

## 红线（绝对禁止，包括"清理测试残留"）

- 一切 DELETE 接口、一切 cancel/作废类接口。需要删除/取消时**只报告，等用户手动**。
  服务器网关对运营 key 也强制 403。建仓无取消接口（作废仅管理员在 ERP 后台）。
- `create-order` 禁带 `force:true`；不直连赛狐/企微外部 API（一律走本机 API 或 mcapi）。
- 不向货代发送承诺发货/议价/付款类内容；只发询价与经用户确认的追问。
- 发货地址店铺独有，缺失=停下让用户补，**绝不借用其他品牌的**。

## 通用规则

- 502 的 detail 是赛狐/亚马逊原始报错，按文本分类处置；HTTP 200 不等于成功
  （generate 返回 `blocked:true` 等要看响应体）。
- 业务系数（×1.13 等）全在 RuleConfig，不手算不写死；改数据走 PUT 接口（自动进
  edited_fields 保护）。业务规则细节见本地 `docs/doc_rules/`（初始化包导入，勿外传）。

## 一、采购计划（触发：拉采购计划/生成采购单/下单/到货）

1. 拉取 `GET /api/sync/purchase-plans?shop=&site=&status=&days=`（shop 收缩写/品牌名；
   报 status_counts 全貌；fetched<sellfox_total 说明截断要缩窗）。状态模型：赛狐计划
   "已采购"只能通过真实建单联动；"已完成"是采购单终态；实时状态以
   `GET /api/purchase/confirm/{pgn}` 的 po_status_label 为准。
2. **开段确认（一次）**：用户点名计划后，先跑 `POST /api/purchase/create-order
   {plan_group_no, dry_run:true}` 预览，把「工厂确认明细 + 仓库/供应商/逐 SKU 单价 +
   action 建议（"2"=提交并下单）」一并复述；供应商空或仓库落默认兜底仓时列为警告。
3. 确认后**连贯执行**：`POST /api/purchase/confirm`（工厂确认落档）→
   `POST /api/purchase/create-order {action, dry_run:false}` → 回查计划已联动"已采购"→
   段尾汇报 purchase_no 与状态。auto_arrival 例外：需单独明示同意（全良品记到货，
   货没到仓会虚增库存）。防重报错→停下报告已有单号，禁 force。
4. 后续动作随叫随做：`POST /api/purchase/submit-order`；到货时用户给良次品数 →
   `POST /api/purchase/arrival`。

## 二、建仓（触发：建仓/STA/分仓/箱唛）——经 mcapi 赛狐建仓，全程两次交互

1. **开段确认（一次）**：备齐并复述——店铺（shop_id 查本机 `GET /api/inbound/shops`，
   由其代理 mcapi `GET /api/v1/sellfox/shops`）、
   明细（`GET /api/inbound/from-purchase-plan/{PPG}` 或 `POST /api/inbound/parse-excel`，
   表格列是英寸/磅，喂赛狐前换算 cm=in×2.54、kg=lb÷2.20462）、箱规（产品库 cm/kg 原生，
   缺则列入本次要补的数据清单）、发货地址（`GET /api/brands` 该品牌 source_address）、
   备货完成时间 ready_to_ship_start 取值。
2. 确认后**连贯执行到停点**：`POST /api/inbound/build/start`
   `{plan_group_no,brand_id,shop_id,items?}`。本机接口内部严格依次执行：mcapi 占坑
   （409=他人已建，停）→ 赛狐建计划（保存返回的 `inbound_plan_id` 和 **owners**）→
   落本机断点记录 → 提交装箱（cm/kg，单箱≤22.7kg，且 quantity=per_box×boxes）→
   生成分仓方案。接口只返回 `placement_options` 和 `requires_selection:true`，**绝不自动确认**。
   呈现各方案仓数/费用/到期时间；`fulfillment_centers` 仅在赛狐确认前可提供时展示，
   为空时明确说明“FC 将在确认方案生成货件后返回”，不可为拿 FC 提前确认。
3. **固定拍板点**：等用户选分仓方案（口径=配合货代报价一起比；过期加 regenerate=true 重出）。
4. 用户点名后**连贯收尾**：`POST /api/inbound/build/{record_id}/finalize`
   `{placement_option_id,ready_to_ship_start}`。本机先校验方案 ID 必须属于当前快照，再经 mcapi
   finalize；默认 FREIGHT_LTL+Other+备货+50天送达窗。特殊承运才直接用 mcapi 细粒度三步
   （`/plans/{id}/placements/confirm` → `/plans/{id}/transport/prepare` →
   `/plans/{id}/transport/confirm`，全部货件一次传齐）→ 更新记录"运输已锁定" →
   `GET /api/sync/plans` 找到 STA → `POST /api/sync/import`
   落批次 → 段尾汇报货件/FBA号，提示可接文件生成。箱唛：mcapi
   `GET /shipments/{amazonShipmentId}/labels`（自送 print_num 必填=箱数）。
5. **断点续跑**：中断后 `GET /api/inbound/plans` 取 record_id/sellfox_plan_id/shop_id →
   `GET /api/inbound/build/{record_id}/remote` 看赛狐实际进度；分仓停点之前用
   `POST /api/inbound/build/{record_id}/resume-to-placement` 接着跑，仍会在选方案处停住。
   已确认过的业务段不重新确认。

## 三、询价（触发：询价/报价/比价/选货代）

1. **开段确认（一次）**：`POST /api/inquiries {batch_id}` 起草后，把正文原文（不得含内部
   编号）+ lanes + 目标货代名单一并展示，用户说"发"即 `POST /api/inquiries/{id}/send`
   连贯完成（**发送无重复防护，status=收集中即已发过，绝不重发**）。改稿 `PUT .../content`。
2. 收集期自动化：`POST /api/qiwe/pull-relay` 拉中继消息；**报价提取直接做**——读原文/看图
   提取后直接 `POST /api/inquiries/{id}/quotes {forwarder_id, lines, currency, raw_ref}`
   落库并汇报提取结果（原文+结构化行对照）；仅当数字含糊/图片看不清时才停下问。
   归属失败的消息列给用户指认后 `POST /api/inquiries/messages/{id}/assign`。
   追问缺仓（外发）仍需用户确认草稿后再发。
3. 比价 `GET /api/inquiries/{id}/comparison`：表格+口径说明（月报关费整月一笔/混币不折算）。
4. **固定拍板点**：用户点名货代后 `POST /api/inquiries/{id}/choose {quote_id}` 并连贯落
   货件（`PUT /api/shipments/{id} {forwarder_id}` 逐货件），汇报边界：仅系统内记录，
   不通知货代。货代单号后续人工回填 `{forwarder_order_no}`（投保前置）。

## 四、文件生成（触发：生成文件/托书/报关/投保/打包）——一次确认跑到底

1. **开段确认（一次）**：`GET /api/batches/{id}/validate` + `GET /api/batches/{id}/full`
   后，一次性复述——①要修的数据清单（error 必修 + warn 影响说明，按 fix_hint 列出
   将执行的 PUT）；②模板清单（默认 suggested_template_ids 全套，**永不传空数组**）；
   ③base_date/contract_date 现值；④将被覆盖的已有文件清单。
2. 确认后**连贯执行**：批量修数据 → 复检 validate 到 passed → `POST /api/batches/{id}/generate
   {template_ids}`（blocked→自动回修再试一轮，仍 blocked 才停）→ 两级采购合同
   `POST /api/batches/{id}/contracts` → `GET /api/batches/{id}/zip` → 段尾汇报：
   生成清单/失败模板及原因（只重传失败的 id）/notes/zip 路径。
3. 段中仅一处硬停：**覆盖用户手工改过的文件**（开段清单里没确认过的新发现）。
   投保单缺货代单号**不拦**（2026-07-29 用户拍板）：置空生成，汇报里逐份提醒
   "单号栏空"，并注明回填 `forwarder_order_no` 后对模板 10 重生成即可覆盖。
4. 抽查建议随汇报给出（`GET /api/docs/{id}/preview` 关键格），不阻塞交付。
   SOP 勾选 `POST /api/batches/{id}/sop` 随做随勾。
