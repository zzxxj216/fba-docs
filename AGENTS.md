# AGENTS.md — Codex/Claude 驱动指南（运营端）

你（AI agent）通过本机 API `http://127.0.0.1:8000` 驱动 FBA 发货流程。服务未起时先启动：
`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`（**严禁 --reload**）。

## 红线（绝对禁止，包括"清理测试残留"）

- 一切 DELETE 接口、一切 cancel/作废类接口（`/api/inbound/plans/*/cancel`、
  `/api/purchase/cancel-order`、`DELETE /api/batches/*`、`DELETE /api/{resource}/*` 等）。
  需要删除/取消时**只报告，等用户在界面/后台手动**。服务器网关对运营 key 也强制 403。
- `create-order` 禁带 `force:true`；不直连赛狐/企微外部 API（一律走本机 API）。
- 不向货代发送承诺发货/议价/付款类内容；只发询价与经用户确认的追问。

## 占坑语义（多运营防重复）

建仓/真实建采购单/确认分仓(live)/外发询价 前系统会自动到服务器占坑：
**遇 RuntimeError"操作冲突：节点已被 X 占用"→ 停下告知用户，绝不重试绕过**（本人重试幂等
放行）。"协调服务不可达"同样停下（fail-closed），让用户检查服务器。

## 通用规则

- 写操作前必须向用户复述关键参数并获明确确认；结果如实汇报（成功/失败/单号）。
- 502 的 detail 是赛狐/亚马逊原始报错，按文本分类处置；HTTP 200 不等于成功
  （generate 返回 `blocked:true`、build 返回 `already_built` 等要看响应体）。
- 业务系数（×1.13 等）全在 RuleConfig，不手算不写死；数据修改走 PUT 接口（自动进
  edited_fields 保护）。业务规则细节见本地 `docs/doc_rules/`（初始化包导入，勿外传）。

## 一、采购计划（触发：拉采购计划/生成采购单/下单/到货）

1. 拉取 `GET /api/sync/purchase-plans?shop=ZE&site=US&status=待采购&days=60`
   （shop 收缩写/品牌名；看 status_counts 报全貌；fetched<sellfox_total 说明截断要缩窗）。
2. 状态模型：赛狐计划"已处理/已采购"**只能**通过真实建单联动（明细 planNo），不能空标；
   "已完成"是采购单终态。实时状态以 `GET /api/purchase/confirm/{pgn}` 的 po_status_label 为准。
3. 门1 工厂确认（用户明示后）`POST /api/purchase/confirm {plan_group_no, items, confirmed_by}`。
4. 门2 建单：先 `POST /api/purchase/create-order {plan_group_no, dry_run:true}` 预览复述
   仓库/供应商/单价（供应商空或仓库落默认兜底仓 → 先补品牌档案）；确认后 dry_run:false，
   action "1"=提交 "2"=提交并下单；auto_arrival 默认 false（虚增库存风险，单独确认）。
   防重报错→报告已有单号，禁 force。成功后回查计划已联动"已采购"。
5. 下单 `POST /api/purchase/submit-order`；到货（用户给良次品数）`POST /api/purchase/arrival`。

## 二、建仓（触发：建仓/STA/分仓/箱唛）——经 mcapi 赛狐建仓接口

建仓执行全在 **mcapi**（`{MCAPI_BASE}/api/v1/sellfox/inbound/*`，请求带 `X-API-Key: {MCAPI_KEY}`）。
本机 API 只做：输入准备、过程记录（断点续跑）、建完导入。**唯一人工停点 = 分仓方案**。

1. 输入准备（本机）：明细来源 `GET /api/inbound/from-purchase-plan/{PPG}` 或
   `POST /api/inbound/parse-excel`（表格列是英寸/磅，喂赛狐前换算：cm=in×2.54，kg=lb÷2.20462）；
   箱规用产品库 cm/kg 原生口径，缺则先补；shop_id 查 mcapi `GET /api/v1/sellfox/shops`；
   发货地址取 `GET /api/brands` 该品牌 source_address（JSON 解析；缺=停下让用户补，**绝不借用他店**）。
2. 防重占坑：mcapi `POST /api/v1/checkpoints {"scope_key":"build:{PPG}","node":"build"}`，
   409=他人已建 → 停下报告，勿重复。
3. 门A（向用户复述店铺/明细/地址并确认后）`POST /plans` {shop_id, name=PPG, source_address,
   items:[{msku,quantity}]}——记下返回的 plan_id 和 **owners**（后面原样回传）；随即
   `POST /api/inbound/records {sellfox_plan_id, shop_id, name, status:"计划已创建"}` 落本机记录。
4. 装箱 `POST /plans/{id}/packing` {box_specs:[{msku,per_box,boxes,length,width,height,weight_kg}],
   owners}（cm/kg；单箱 ≤22.7kg）→ 记录"装箱已提交"。
5. 停点 `GET /plans/{id}/placements`（方案过期加 regenerate=true）→ 呈现各方案仓数/FC/费用 →
   **停住等用户选**（口径=配合货代报价一起比）→ 记录"分仓方案已生成"。
6. 门B（用户点名方案后）`POST /plans/{id}/finalize` {placement_option_id, shop_id,
   ready_to_ship_start 如 2026-08-04T00:00Z}——默认 FREIGHT_LTL+Other、送达窗自动选备货+50天，
   返回货件清单 → 记录"运输已锁定"+shipments。特殊承运/窗口用细粒度三步：
   /placements/confirm → /transport/prepare → /transport/confirm（全部货件一次传齐）。
7. **断点续跑**：中断后 `GET /api/inbound/plans`（本机记录）取 sellfox_plan_id/shop_id →
   mcapi `GET /plans/{id}?shop_id=` 看实际进度 → 从对应步骤接着做；每推进一步都 upsert records。
8. 建完导入（文件撰写的前置）：`GET /api/sync/plans` 找到该 STA →
   `POST /api/sync/import {"inbound_plan_id":…}` 落批次（货件/FBA号/明细自动进来）→
   记录"已导入批次" → 转「文件生成」章。
9. 箱唛 mcapi `GET /shipments/{amazonShipmentId}/labels`（自送货件 print_num 必填=箱数）；
   货件摘要（询价口径）`GET /shipments/{amazonShipmentId}`。
红线：建仓无取消接口（作废仅管理员在 ERP 后台人工）；发货地址店铺独有严禁串用。

## 三、询价（触发：询价/报价/比价/选货代）

1. 起草 `POST /api/inquiries {batch_id}`——复述正文（**不得含内部编号**）/lanes/目标货代。
2. 外发闸门：用户明说"发"才 `POST /api/inquiries/{id}/send`；**发送无重复防护，
   status=收集中即已发过，绝不重发**。改稿 `PUT .../content`。
3. 收集：`POST /api/qiwe/pull-relay` 从服务器中继拉新消息进本地 → 归属失败的看
   `GET /api/inquiries/messages/pending`，让用户指认后 `POST /api/inquiries/messages/{id}/assign`。
4. **报价提取由你完成**（读原文/看图）：提取后先向用户复述"原文 + 每行 FC/单价/单位"，
   确认后 `POST /api/inquiries/{id}/quotes {forwarder_id, lines:[{fc,price,unit,...}],
   currency, customs_fee_monthly, raw_ref:原文}`（按 FC 增量合并，返回 missing 缺仓）。
   含糊不猜；追问草稿（`POST .../followup`）发送前提醒用户删 ref_code。
5. 比价 `GET /api/inquiries/{id}/comparison`：表格呈现+口径说明（月报关费整月一笔/混币
   不折算），推荐仅供参考。用户点名后 `POST /api/inquiries/{id}/choose {quote_id}`——
   仅本地记录，不通知货代、不触发亚马逊。
6. 衔接：选定货代落货件 `PUT /api/shipments/{id} {forwarder_id}`；货代单号人工回填
   `{forwarder_order_no}`（投保前置）；运输确认走建仓门B。

## 四、文件生成（触发：生成文件/托书/报关/投保/打包）

1. `GET /api/batches/{id}/validate`：error 必须修（fix_hint→对应 PUT 接口，逐条经用户确认
   不猜值）；warn 念给用户拍板。
2. 参数门：模板集以 /full 的 suggested_template_ids 为底逐个确认（**永不传空数组**）；
   base_date/contract_date 确认（改了目录名会变）；投保单缺货代单号会置空生成要拦；
   重生成覆盖同名文件先问。
3. `POST /api/batches/{id}/generate {template_ids}`：blocked→回校验；逐模板报 errors/notes；
   只重传失败的模板 id。两级采购合同 `POST /api/batches/{id}/contracts`（persist:false 试跑）。
4. 核对 `GET /api/docs/{id}/preview` 抽查 → `GET /api/batches/{id}/zip` 交付 →
   `POST /api/batches/{id}/sop` 勾手动步骤。
