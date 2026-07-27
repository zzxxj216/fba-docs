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
   仓库/供应商/单价（供应商空或仓库=默认56115 → 先补品牌档案）；确认后 dry_run:false，
   action "1"=提交 "2"=提交并下单；auto_arrival 默认 false（虚增库存风险，单独确认）。
   防重报错→报告已有单号，禁 force。成功后回查计划已联动"已采购"。
5. 下单 `POST /api/purchase/submit-order`；到货（用户给良次品数）`POST /api/purchase/arrival`。

## 二、建仓（触发：建仓/STA/分仓/箱唛）

1. 预检：批次定位（`GET /api/batches` → `/api/batches/{id}/full`）；`GET /api/batches/{id}/prep`
   ready；品牌 source_address 完整（**缺了停下让用户补，绝不代填**）；amazon_store 复述确认；
   inbound_plan_id 有值且方案空 = 卡死态只报告。
2. 门A（确认后）`POST /api/batches/{id}/build`（真实写亚马逊，数分钟，--max-time≥900；
   超时≠失败先查 /full；报错含"草稿计划待手动取消"→原样转告）。
3. 方案报告：从 /full 读 placement_options 按费用排列。**停住**——口径=placement 费+货代
   报价总成本一起比，先走询价；单方案也不自动确认。
4. 门B：先 `POST /api/batches/{id}/confirm-placement {placement_option_id, live:false}` 演练
   （告知会重排本地货件行；materialize_error 要念出）→ 用户说"真实提交"才 live:true
   （最坏 20-30 分钟后台跑）→ 必须逐货件核对 /full 里 amazon_shipment_id 回填，空=报告。
5. 门C 标签：`POST /api/batches/{id}/labels {kind: box|fnsku, live:false→true}`，核对张数=箱数。

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
