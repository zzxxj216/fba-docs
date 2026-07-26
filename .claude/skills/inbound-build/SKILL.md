---
name: inbound-build
description: 亚马逊 FBA 建仓（API 线店铺 ZE/RA/BY 等）：批次建仓出分仓方案、人工拍板后确认分仓+配自送运输、下载箱唛/FNSKU 标签。触发词：建仓、STA、入库计划、分仓、确认分仓、箱唛、标签、运输配置。
---

# 建仓 skill（批次线，官方 SP-API 通道）

驱动流程：**定位批次 → 预检 → 建仓（出分仓方案）→ 停等拍板 → 确认分仓+自送运输(live) → 箱唛标签**。
一律走中转站 `http://127.0.0.1:8000`（curl），mcapi(8100) 只允许只读探活，**绝不调 mcapi 写接口**。
向导线 `/api/inbound/*` 是遗留，不使用（单装箱组/无 EU 支持/无纠错）。

## 红线（绝对禁止，包括"清理测试残留/重建前清理"）

- `POST /api/inbound/plans/{id}/cancel`（取消亚马逊入库计划）
- `DELETE /api/batches/{id}`（级联删批次+文件）、一切 DELETE/cancel 类接口
- mcapi `PUT /inbound-plans/{id}/cancel` 及 mcapi 一切写接口
- 建仓失败留下的**草稿入库计划只报告 planId 等用户手动取消**（报错消息里会带
  "本次留下草稿入库计划待手动取消：…"，必须原样转告用户）。

## 前置检查（门 A 之前全部过一遍）

1. 中转站：`GET /api/brands` 通（不通则在 D:\amazon 后台启动
   `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`，**严禁 --reload**）。
2. 定位批次：`GET /api/batches` 按 name/purchase_plan_no 找，`GET /api/batches/{id}/full` 拿全貌。
   没有批次时从赛狐采购计划建：`POST /api/sync/purchase-plans/import-only {plan_group_no}`。
3. 数据体检：`GET /api/batches/{id}/prep` → `ready` 必须 true（缺档先
   `POST /api/batches/{id}/prep/fill-products`，仍缺让用户补产品库；用户明示接受带病继续除外）。
4. 品牌档案（从 GET /api/brands 找该批次品牌）：
   - `source_address` 是 JSON 字符串，解析后 `address_line1` 必须非空——缺了就停，
     让用户去「主体与品牌」补，**绝不复制别家地址应急**（曾出店铺串联事故）。
   - `amazon_store` 空串=默认店 main，不是错误；但要在门 A 复述给用户确认是哪个店。
5. mcapi 探活：`GET http://127.0.0.1:8100/api/v1/amazon/fba/inbound-plans?page_size=1&store={amazon_store}`。
   **EU 批次（country ∈ DE/FR/IT/ES/NL/SE/PL/BE/IE/UK/GB）store 要加 `_eu` 后缀再探**，
   与建仓实际用的凭据一致。不通 → 停，报告让用户启动 mcapi
   （`cd F:\练习模块\multi-channel-api && python -m uvicorn app.main:app --port 8100`，不代起）。
6. `batch.inbound_plan_id` 分流：
   - 空 → 正常走门 A。
   - 有值且 placement_options 非空 → 已建仓，直接跳到「展示方案」。
   - 有值但 placement_options 空 → **卡死态**（半途失败）：报告等人工
     （用户手动取消亚马逊草稿后清 inbound_plan_id），绝不重试、绝不清理。

## 门 A：建仓（真实写亚马逊，无 dry-run）

向用户复述并等明确确认：批次名 / 品牌+店铺 store（EU 加 _eu）/ 国家 / SKU 行数 /
总件数箱数 / 发货地址摘要。确认后：
```
POST /api/batches/{id}/build      # curl --max-time 900，或 run_in_background
```
- 耗时数分钟（create 纠错最多 8 轮 + packing 纠正 20 轮 + placement 生成 ≤300s）。
- 返回统一 dict：正常 `{inbound_plan_id, option_count}`；已建仓
  `{inbound_plan_id, option_count, already_built: true}`（already_built 且 option_count=0 = 卡死态）。
- 502 detail 分类：含"mcapi 已在…运行"→启 mcapi；含"缺 每箱数/箱长"→补产品库；
  含"未配置发货地址"→补店铺档案；含"拒绝了这些 SKU/没映射"→配 amazon_store；
  含"草稿入库计划待手动取消"→**原样报告 planId 等用户清理**；
  纠错循环不收敛→停下报原始错误，不自行构造参数硬试。
- 超时≠失败：客户端断开后服务端继续跑，先 `GET /api/batches/{id}/full` 查状态再决定，勿盲目重发。

## 展示方案 → 停等拍板

从 `GET /api/batches/{id}/full` 读 `placement_options`（勿依赖 build 返回体），按 fee_usd
升序呈现：每方案 label(N 仓)/分仓费/逐 FC 的件·箱·重量。**明确告知业务口径：选方案要按
"placement 费 + 货代头程报价"的总成本一起比，通常先走询价 skill 拿到报价再拍板；
即使只有一个方案或费用为 0 也不自动确认。** skill 在此停住等用户点名 placement_option_id。

## 门 B：确认分仓 + 自送运输（不可逆——正式生成货件）

1. **先演练**（用户选定方案后）：
```
POST /api/batches/{id}/confirm-placement   {"placement_option_id": "...", "live": false}
```
   - 调用前告知：**演练也会改本地数据**——把"未分仓"货件行删除重建为逐 FC 行并落库
     （批次已是逐 FC 货件则自动 skipped，不动数据）。只对已拍板的方案演练，不拿演练试算多方案。
   - 返回后复述 fcs/steps/materialized；**响应里有 `materialize_error` 必须念出来**，不能只看 200。
2. **真实提交**（用户明确说"真实提交"后才做）：
```
POST /api/batches/{id}/confirm-placement   {"placement_option_id": "...", "live": true}
```
   - 长耗时：自送运输（USE_YOUR_OWN_CARRIER+LTL+OTHER）采样最多 30 轮，最坏 20-30 分钟。
     用 run_in_background + `curl --max-time 3600`，期间不轮询不重发。
   - 失败"采样 30 次仍无法…同时拿到 OTHER-LTL"是亚马逊侧概率问题：可询问用户是否重试
     （幂等，already 类错误自动豁免）。
3. **验证回填**（必做，不能凭 200 判成功）：`GET /api/batches/{id}/full` 逐货件核对
   `amazon_shipment_id`(FBA 号)、`reference_id`、收货地址已落库，batch.status=运输已配置。
   任一货件 FBA 号为空 → 明确报告（服务端只打印警告不抛错，历史上漏填过两次）。

## 门 C：箱唛 / FNSKU 标签

1. 演练：`POST /api/batches/{id}/labels {"kind": "box", "live": false}`（fnsku 同理；
   page_type 用户没指定就不传，箱唛默认热敏 PackageLabel_Thermal、FNSKU 默认 LETTER_30）。
2. 用户确认后 `"live": true` 真实下载：返回 total_labels/zip/merged_pdf/out_dir。
   **核对 saved[].boxes 与本地各货件 carton_num 一致**（>20 箱翻页截断有事故前科，
   服务端已修复但必须复核张数）。产物在 `output/{批次名-MMDD}/labels/`。

## 收尾

汇总：入库计划 planId / 选定方案 / 逐货件 FBA 号+FC / 标签数与路径；SOP build 步会按
"货件有 fc_code"自动判定完成。提示下一步：货代询价（forwarder-inquiry skill）、
生成文件（doc-generate skill）。
