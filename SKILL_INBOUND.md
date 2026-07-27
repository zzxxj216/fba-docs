# 建仓 Skill 规划（草案 v1 + v2 建仓前准备，2026-07-26）

> **v2 说明（同日追加）**：架构升级为"局域网中转站 + 多运营对话 + 双通道建仓 + 去飞书"。
> §1-§7 是 v1（单机 skill、仅官方 API 通道）的盘点结论，事实部分仍然有效；
> 流程与改造清单以 **§8 起的 v2 为准**（v1 改造①-④全部保留并入 §8.8）。

把"建仓"（亚马逊 FBA STA 入库计划创建，经 mcapi:8100 调 SP-API）做成 Claude Code 项目级 skill，
让 Claude 在对话里按 SOP 驱动全流程：定位/创建批次 → 预检 → 建仓 → 分仓方案报告 → 等用户拍板 →
live 确认分仓+自送运输 → 箱唛标签 → 汇总。本文是实施前的规划，含代码改造清单与权限方案。
基于 2026-07-26 对 `app/routers/inbound.py`、`app/services/inbound_service.py`、`app/amazon_fba_client.py`、
mcapi（F:\练习模块\multi-channel-api）、`static/app.js`、`app/sop_flow.py`、`.claude` 配置的全面盘点。

---

## 1. 关键结论（决定 skill 形态的事实）

1. **建仓有两条并行实现，skill 只走批次线。**
   - 批次线：`POST /api/batches/{id}/build` → `build_for_batch`（inbound_service.py:611-824）。
     一口气 create→packing→boxes→placement，带 owner/效期自动纠错（8+20 轮）、多装箱组、EU
     destination_marketplaces 处理，方案落 `batch.placement_options`。健壮，且与 SOP 进度条、
     后续文件生成打通。
   - 向导线：`/api/inbound/*` 逐步接口 + InboundPlan 表。只支持单装箱组（:436）、无 EU 支持、
     无纠错、`InboundPlan.batch_id` 从未回填。**视为遗留，不进 skill**（补仓 Excel 入口在向导线上，
     列为 phase 2）。
2. **建仓后半段（确认分仓+自送运输 `confirm_placement_for_batch`、箱唛 `fetch_labels`）没有 REST
   路由**，只挂在飞书卡片动作上（可经调试端点 `POST /api/feishu/simulate/action` 触发），且 live
   门控是**进程级环境变量** `INBOUND_LIVE_SUBMIT=1`（feishu/service.py:729,892）——HTTP 侧无法按次
   传 live；开了它飞书侧所有按钮同时变 live，影响面全局。→ 必须先做改造①。
3. **`POST /api/batches/{id}/build` 没有 dry-run 闸门**，一调即真实在亚马逊创建入库计划
   （routers/batches.py:158-168）。→ skill 必须在调用前设人工确认门 A。
4. **权限现状零护栏**：全局 `defaultMode=bypassPermissions`，项目级 `.claude/` 无任何
   settings/skills/hooks。红线（禁 cancel/delete）目前完全靠自觉。→ 见 §5 权限方案。
5. **预检基础设施是死代码**：`store_profile_service.check_store`（能查店铺档案完整性+按店探
   mcapi 凭据）无任何路由挂载；`amazon_fba_client.ping()` 同样无人调用。→ 改造②（或降级方案）。
6. 已知坑（skill 的 troubleshooting 必须覆盖）：
   - build 半途失败卡死态：`inbound_plan_id` 有值但 `placement_options` 空 → 幂等护栏
     （:618-622）让重跑静默返回 `[]`，无法自动恢复，只能人工。
   - build 响应两种形状：正常 `{"inbound_plan_id", "option_count"}`；已建仓短路返回 list
     `[]`/方案数组。分仓报告统一从 `GET /api/batches/{id}/full` 读，不依赖 build 返回体。
   - 失败重试留下的亚马逊草稿计划 ID 只 print 到服务器控制台（:690-691），HTTP 层拿不到 → 改造④。
   - `import-only` 的国家映射 `_COUNTRY` 只有 美/英/加/德/日 五项（purchase_plan_service.py:530），
     FR/IT/ES/NL/MX 等会把中文站名写进 `batch.country`，EU 凭据后缀与 marketplace 全失效 → 改造③。
   - 重复 import-only 不刷新已有批次明细（purchase_plan_service.py:538,571），采购计划改量后
     只能报告，不能重导修复。
   - dry-run 非零副作用：`confirm_placement_for_batch` 演练阶段就会 `materialize_placement`
     重排本地货件行（:895-899），且换方案重预览会被 fc_code 保护 skip——**只对用户已拍板的方案做
     dry-run→live，不拿 dry-run 试算多方案**。
   - 长耗时：build 全程 5-10 分钟；live 确认含自送采样最多 30 轮（5 仓同时命中 OTHER-LTL 约
     14%/次），可能十几分钟。skill 调用一律 run_in_background + 长超时，**超时≠失败**，先查批次
     状态再决定下一步（幂等短路保证重查安全）。

## 2. Skill 流程设计（三道确认门）

```
Step 0 预检 ──► Step 1 定位/创建批次 ──► Step 2 数据体检 ──► [门A] Step 3 建仓
──► Step 4 分仓方案报告（停住，等货代报价+用户拍板） ──► [门B] Step 5 确认分仓+自送运输
──► [门C] Step 6 箱唛/FNSKU 标签 ──► Step 7 汇总报告 + SOP 联动
```

**Step 0 预检**
- 主系统：`GET /api/brands`（兼健康检查 + 预检数据源，返回 amazon_store/source_address）。
  未启动 → skill 可后台拉起 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  （**严禁 --reload**，start.bat:5 安全软件卡死警告）。
- mcapi：`GET http://127.0.0.1:8100/health`（进程活）→ 按目标店只读探活
  `GET /api/v1/amazon/fba/inbound-plans?page_size=1&store=<store>`（凭据通）。
  未启动 → **只报告 + 给命令**让用户手动启（`cd F:\练习模块\multi-channel-api &&
  python -m uvicorn app.main:app --port 8100`），skill 不代管其生命周期
  （mcapi 有 token 回写 .env 等后台副作用；README 写的 8000 端口是错的会撞主系统）。

**Step 1 定位/创建批次**
- `GET /api/batches` 全量拉取，按 `name`/`purchase_plan_no` 过滤（无搜索参数，量级可接受）。
- 没有批次 → `POST /api/sync/purchase-plans/import-only {plan_group_no}` 从赛狐采购计划建批次。
- 已存在同 purchase_plan_no 批次且数量对不上 → 只报告（重导不刷新明细）。

**Step 2 数据体检**
- `GET /api/batches/{id}/prep` → `ready` 布尔 + `issues` 数组，机器可判读。
- 缺 SKU 档案 → `POST /api/batches/{id}/prep/fill-products` 自动从赛狐补，再复检。
- 注意：prep.ready 不查品牌绑定/发货地址/店铺账号——门 A 单独校验。

**门 A（建仓前，硬校验五项 + 用户确认）**
1. `prep.ready == true`（或用户明示接受带病继续）
2. `batch.brand_id` 非空（import-only 品牌名精确匹配不上会是 None）
3. 对应 Brand 的 `source_address` 完整 + `amazon_store` 非空
   （发货地址店铺独有严禁回退——Byane 用了 HUHOLE 地址的事故；store 为空会静默走默认店 main）
4. `batch.country` 是标准两位码（防国家映射缺失导致 EU 凭据/marketplace 失效）
5. `batch.inbound_plan_id` 为空。有值 → 分流：`placement_options` 非空 = 已建仓，直接进 Step 4；
   为空 = **卡死态**（半途失败）或赛狐同步批次，报告等人工，绝不重试/清理/取消。

确认话术必须复述：批次名、品牌+店铺 store（+是否 _eu）、国家/marketplace、SKU 行数、
总件数/箱数、发货地址摘要。用户确认后才 `POST /api/batches/{id}/build`。

**Step 4 分仓方案报告**
- 从 `GET /api/batches/{id}/full` 读解析好的 `placement_options`，输出：每方案仓数/逐 FC
  件·箱·重量/分仓费，分仓 vs 合仓对比。
- 明确业务口径：**方案选择 = placement 费 + 货代头程报价的总成本一起比**（workspace.py:148-149），
  不是自动选最便宜。skill 在此停住，提示可先走货代询价（AGENT_FORWARDER），等用户拍板。

**门 B（确认分仓+自送运输，live 写亚马逊，不可逆——正式生成货件）**
- 用户明示选定 placement_option_id 后才执行；依赖改造①的 REST 路由按次传 live。
- **改造①合入前，此步只做 dry-run 预览 + 指引用户走前端/飞书**，绝不引导用户全局开
  `INBOUND_LIVE_SUBMIT=1`。
- 自送规则（引用代码现状，非 skill 写死）：USE_YOUR_OWN_CARRIER + FREIGHT_LTL + OTHER 承运，
  readyToShipWindow=本周五+50 天，采样上限 30 次，confirm 一次带全部货件。采样耗尽不收敛 →
  停下报告，不盲目重试（每次 generate 都是真实写）。
- 成功后 `_backfill_shipments` 自动回填 FBA 确认号/reference/FC 地址，batch.status=运输已配置。

**门 C（箱唛/FNSKU 标签）**
- 用户同意后 `fetch_labels`（改造①的 labels 路由），kind=box/fnsku。
- 已内置的坑：>20 箱翻页（RazEdg 事故）、自送货件 page_size 必填=箱数、热敏裁剪。
- 产物在 output/{批次}/labels，报告 zip/PDF 路径。

**Step 7 汇总**
- 逐货件 FBA 号/FC/箱数、标签路径、SOP build 步自动判定（货件有 fc_code 即完成）、
  下一步指引（货代询价、生成文件 /generate）。

## 3. 失败处置手册（skill references/troubleshooting.md 的骨架）

| 症状 | 判别 | 处置 |
|---|---|---|
| 502 "mcapi 连接失败…确认已在 8100 运行" | detail 关键词 | 指引启动 mcapi，重试该步 |
| 502 "SKU xxx 缺 每箱数/箱长…" | detail 关键词 | prep/fill-products 或让用户在产品库补 |
| 502 "品牌…未配置发货地址" | detail 关键词 | 指引补店铺档案，**绝不复制别家地址** |
| 502 "SKU not valid"+提示配 amazon_store | detail 关键词 | 指引「主体与品牌」配店铺映射/mcapi 凭据 |
| 502 纠正循环不收敛（8/20 轮耗尽） | detail | 停下报告原始报错，不自行构造参数硬试 |
| build 返回 200 但空 list | 响应形状 | = 已建仓/同步批次，从 /full 读方案或走已建仓分支 |
| inbound_plan_id 有值 + placement_options 空 | 卡死态 | 报告等人工（用户手动取消亚马逊草稿+清字段），skill 不动 |
| curl 超时 | — | 超时≠失败：先 `GET /api/batches/{id}/full` 查状态再决定 |
| 亚马逊草稿计划残留 | build 失败后 | 只报告（改造④后含 planId），等用户手动取消 |

**红线（绝对禁调，任何场景包括"清理测试残留"）**
- `POST /api/inbound/plans/{id}/cancel`（routers/inbound.py:119）
- `DELETE /api/batches/{id}`（routers/batches.py:205）
- `DELETE /api/{resource}/{item_id}` 通用删除（crud.py:146，品牌/产品/规则都能删）
- mcapi `PUT /api/v1/amazon/fba/inbound-plans/{id}/cancel` 及 mcapi 一切写接口（skill 对 8100
  只允许 /health 和只读探活）

## 4. 前置代码改造清单（做 skill 之前完成，均为小改）

| # | 改造 | 位置 | 说明 |
|---|---|---|---|
| ① | REST 化建仓后半段 | routers/batches.py | `POST /api/batches/{id}/confirm-placement {placement_option_id, live}`、`POST /api/batches/{id}/labels {kind, live}`。live 显式按次传参；飞书通道保留 INBOUND_LIVE_SUBMIT 门控不动。建议后台任务+轮询或明确超时语义（自送采样可超 10 分钟） |
| ② | 预检路由 | routers 新增 | 挂现成死代码 `store_profile_service.check_store/check_all` + `fba.ping()`（如 `GET /api/store-profile/check/{brand_id}`）。降级替代：skill 用 `GET /api/brands` 自查档案 + 直连 8100 只读探活，则②可缓 |
| ③ | 国家映射补全 | purchase_plan_service.py:530 | `_COUNTRY` 补 FR/IT/ES/NL/SE/PL/BE/IE/MX 等（一行） |
| ④ | 失败信息透出 | inbound_service.py:690 | 草稿 planId 收集进 RuntimeError 消息；build 已建仓短路改返回 `{"already_built": true, "inbound_plan_id", "option_count"}` 统一形状 |

（已砍：build 纯校验模式——prep + 门 A 五项自查已覆盖。）

## 5. 权限方案（三层纵深，现状 bypassPermissions 下 deny 单独不够）

1. **PreToolUse hook（主护栏）**：`.claude/settings.json` 配 hook 脚本，检查 Bash 命令文本，
   命中 `cancel`/`DELETE` 动词 + 8000/8100 URL 模式即 exit 2 阻断。hooks 在所有权限模式
   （含 bypassPermissions）下都执行，比 deny 语义可靠。
2. **permissions.deny（第二层）**：项目级 deny `Bash(*/cancel*)`、`Bash(*-X DELETE*)` 等模式。
   承认字符串匹配脆弱（变量拼 URL、python 内 httpx 都绕得过），属纵深防御。
3. **SKILL.md 明文白名单（第三层）**：
   - 允许：`GET /api/batches`、`GET /api/batches/{id}/full|prep|validate`、
     `POST /api/batches/{id}/prep/fill-products|build|（改造后）confirm-placement|labels`、
     `GET/POST /api/sync/purchase-plans*`、`GET /api/brands`、`GET /api/inbound/plans*`（只读）、
     mcapi 仅 `GET /health` + 只读探活。
   - 禁止：§3 红线清单全部；`/api/feishu/simulate/action` 仅在改造①未落地的过渡期用于 dry-run
     预览（固定假 chat_id，防真发飞书卡片）。
4. 不引导用户把 uvicorn host 改 0.0.0.0（全 API 无鉴权，只靠 127.0.0.1 绑定兜底）。

## 6. Skill 包结构

```
.claude/skills/inbound-build/
  SKILL.md                    # 主 SOP：触发条件、流程+三道门、确认话术、红线
  references/
    endpoints.md              # 接口白名单/黑名单 + 请求响应形状（含 build 双形状）
    troubleshooting.md        # §3 失败处置手册全文
```
- 触发：用户说 "建仓 <批次/采购计划号>"、/inbound-build。description 写清触发词：
  建仓/STA/入库计划/分仓/补仓计划。
- 内容原则：**不写死业务常量**（周五+50 天、+1 年效期、×1.13 等随业务变），一律"引用系统当前
  实现"并指向代码/RuleConfig；报关系数出现具体数字即视为 bug。
- SKILL.md 保持精简（流程+门+红线），细节下沉 references/ 按需加载。

## 7. 待拍板（均带推荐）

| # | 问题 | 推荐 |
|---|---|---|
| 1 | 门 B live 授权模型 | 改造①按次传 live=true，且 skill 必须在对话中逐批次经用户明示确认；不依赖全局环境变量 |
| 2 | mcapi 生命周期 | skill 只报告+给命令，不代起代停（主系统 8000 可代起） |
| 3 | 权限 hook 是否上 | 上（bypassPermissions 下唯一可靠护栏），deny 作第二层 |
| 4 | 补仓 Excel 入口 | ~~phase 2~~ → v2 提前实现（RPA 通道主输入），见 §8.3 |
| 5 | 向导线 /api/inbound/* 去留 | 标记遗留；skill 不用，前端暂保留 |
| 6 | 分仓确认是否必须等货代报价 | 按现有口径：报告方案后停住，等报价+用户拍板；即使只有 1 个方案也不自动确认 |

---

# v2：局域网中转站 + 双通道建仓 —— 建仓前准备规划（2026-07-26）

> **⚠️ 2026-07-27 架构再调整，"中转站"模型作废**：fba-docs 不做中央部署，改为
> **开源仓库 + 运营各自本地实例 + mcapi 作服务器网关 + 中央 MySQL**，见 `OPENSOURCE_PLAN.md`。
> 本章的双通道路由（§8.2）、数据来源（§8.3）、预检清单（§8.4）等业务设计仍然有效，
> 但部署形态/鉴权方案以 OPENSOURCE_PLAN.md 为准。

## 8. 架构定位

```
运营A/B/C 各自电脑（Claude Code / Codex 对话 + 本地记录文件）
        │  LAN HTTP（API Key 分级，见改造⓪）
        ▼
D:\amazon 中转站（FastAPI:8000，Windows）＝唯一业务入口/数据权威
        ├─► mcapi:8100（本机回环，官方 SP-API 通道，不对局域网暴露）
        ├─► 紫鸟 Mac:8848（ziniao-premium-aplus-poc 中心服务，RPA 通道，X-API-Key）
        └─► 赛狐 OpenAPI（数据源：采购计划/商品库/回流同步）
```

- 飞书退役：其"内部运营界面层"职责由 **运营对话（确认门）+ 本地前端** 承接，清单见 §8.7。
  货代沟通走企微 qiweapi，与飞书无关，**不受影响**（已核实 AGENT_FORWARDER.md + qiwe_client）。
- RPA 仓库已经是"中心服务 HTTP + 运营端瘦技能"模式（operator-skill 先例），与中转站架构天然同构，
  建仓 RPA 通道照搬该模式，运营心智不变。

## 8.2 双通道路由（店铺→通道，DB 现状已核实 2026-07-26）

| 缩写 | Brand（id） | amazon_store | 默认通道 | 现状备注 |
|---|---|---|---|---|
| BY | Byane (1) | byane | api | 地址已配 |
| ZE | Zentop (2) | zentop | api | 地址已配 |
| RA | RazEdg (3) | razedg | api | 紫鸟侧也有 FBA 实测记录（可作 RPA 备用） |
| SE | Serenorch (6) | serenorch | rpa | **紫鸟未添加该店**，首跑需人工陪跑（2FA/页面差异） |
| HU | HUHOLE (7) | **qifengz**（非直觉命名，勿按品牌名猜） | rpa | 紫鸟已实测 |
| BF | BFPeaky (8) | bfpeaky | rpa | **紫鸟未添加该店**，首跑需陪跑 |
| XINGNEST | **无 Brand 记录** | — | rpa | 目前只是境外收货人（RuleConfig company:8/9 "XING NEST CO., LIMITED"）；建店需拍板绑定主体，且报关规则 `company.id in (8,9)` 硬编码（field_registry.py:282）要同步扩 |

- 路由实现：`Brand.inbound_channel`（api|rpa，改造⑤）为默认；"大多数时候"= 会话内可按次覆盖，
  覆盖必须在门 A 复述并经运营确认。
- 通道分叉的预检也分叉：`store_profile_service` 的 amazon_store/mcapi 探活检查是 API 线专属，
  RPA 品牌跳过，改查紫鸟侧前置（§8.4）。
- Cenforge/Pexwo（amazon_store、地址均空）不在两通道清单内，建仓请求直接拒绝并提示补档。

## 8.3 数据来源两条 → 统一落批次（建仓的唯一起点=Batch）

**来源A：赛狐采购计划**（现成）——`POST /api/sync/purchase-plans/import-only {plan_group_no}`，
status=数据准备。注意重复导入不刷新明细、国家映射缺失（改造③）两坑不变。

**来源B：运营上传补仓 Excel**（新端点，改造⑥，RPA 通道主输入）：
`POST /api/inbound/import-excel`，multipart(file, **brand_id 必填**（Excel 无店铺列）, country?, name?, operator?)
- 解析复用 `parse_replenishment_excel`（列关键词匹配，in/lb 直读）；
- 建批仿 `import_plan_only` 骨架：Batch(status=数据准备, channel=按品牌, purchase_plan_no=`excel-{文件hash12}` 幂等)
  + 单个 fc_code='' Shipment(待建仓) + Items（箱规 in×2.54 存 cm，与 ShipmentItem 口径一致）；
- base_date/contract_date 按规则现算（下周五/上月同日，同 sync_service 先例）；
- 产品缺档：**本地空建档+进校验报告**，纯 Excel 通道不调赛狐 `_auto_create_product`；
  缺品名/成本/HS 会阻断后续文件生成（CLAUDE.md 第 5 条），prep 报告里明示。

**RPA 建仓完成后的回流与配对**（改造⑨）：RPA 建完仓后从赛狐导出"FBA货件"xlsx 走现有
`POST /api/sync/import-excel`（幂等键 `offline-{hash}`）或等赛狐同步。与 Excel 预建批次
（`excel-{hash}`）幂等键互不相认——需补配对逻辑（建议：回流时运营在对话里指定 batch_id 合并，
自动匹配作辅助），否则同一批货出双 Batch。

## 8.4 建仓前准备预检清单（skill 的 Step 0-2，按通道分叉）

**共通（两通道都做）**
1. 中转站 8000 存活（GET /api/brands，兼预检数据源）。
2. 批次定位/创建（§8.3 两来源），复述批次名/品牌/国家/明细汇总。
3. `GET /api/batches/{id}/prep` 体检 ready，缺档先 `POST .../prep/fill-products`（赛狐来源）或
   报告清单让运营补（Excel 来源）。
4. `batch.brand_id` 非空；`batch.country` 标准两位码。
5. 运营身份落批次（Batch.operator，改造⑤），本地记录文件初始化（§8.5）。
6. `batch.inbound_plan_id` 为空（有值走已建仓/卡死态分流，同 v1 门 A 第 5 项）。

**API 通道追加（ZE/RA/BY）**——即 v1 门 A：
mcapi /health + 按店只读探活；`amazon_store` 非空；`source_address` 完整（严禁回退）；EU 站 _eu 凭据。

**RPA 通道追加（SE/BF/HU/XINGNEST）**
1. 紫鸟 Mac `GET :8848/health` + `GET /aplus/status` 忙闲（单任务串行，忙则排队报告，**与 A+ 制作共用一把锁**）。
2. 店铺已在紫鸟添加且登录态可用（SE/BF 目前否——首跑必须人工陪跑过 2FA，参考 Xingnest 先例，
   陪跑结论回填 RPA 仓库 fba_store_notes.md）。
3. 生成 spec.json（sku/units_per_box/boxes/quantity/expiration?；**箱规四项 box_l/w/h_in+weight_lb 必须全**，
   缺一项亚马逊整文件拒收）+ box_specs.json（cm/kg 原值，询价折算用）；发任务前先过
   `validate_fba_spec` 同构校验。换算走 rule_engine 不硬编码。
4. 确认完成口径（待拍板：停 Step3 还是一条龙到 Step4 tracking 页前）。
5. 明确 RPA 失败分类：listing 同步延迟（新品 1-2h）/stranded → **等待/人工**，不重试；
   废工作流残留 → 只报告 wf 编号等人工 void（RPA 代码自带此红线，与本系统一致）。

## 8.5 运营身份与对话记录

- **身份**：改造⓪的 API Key 每运营一把（key→operator 名），或过渡期 `X-Operator` 头
  （先例：purchase 的 confirmed_by body 透传）。`feishu_operators` 表改造为渠道中立 `operators`
  （去 feishu_open_id 主键化，**保留 scope_brand_ids 管辖模型**——现有数据仅 1 行 Zane/[6,7]）。
  Batch.operator / InboundPlan.operator 落库（改造⑤）。
- **对话记录（需求③）双层分工**：
  - 运营本机 = 对话台账：skill 开场在本机建 `建仓记录/{日期}-{批次名}/日志.md`，逐步追加
    （时间戳/动作/确认记录/方案/结果），会话结束归档——这满足"内容都保存在运营A电脑"。
  - 服务端 = 事实源：DB + `output/工作区/{运营}/{周}/` 中心视图。现有 `app/feishu/workspace.py`
    **零飞书依赖（纯文件系统，state.json 唯一事实源）**，平移为 `services/workspace_service.py`
    即可复用（改造⑧），写入点从飞书动作挪到各业务函数成功/失败处。

## 8.6 RPA 通道集成设计（基于 ziniao-premium-aplus-poc 2026-07-26 现状）

- 现状：Mac 上 FastAPI:8848（X-API-Key、单任务串行 409、超时 1200s terminate）；
  **HTTP 端点 /fba/create-shipment 只到 Step1/2 dump；打通 Step4 的 fba_pipeline.py 是 CLI（环境变量驱动）未封 HTTP**。
- 改造⑦（RPA 仓库侧）：把 fba_pipeline 封成**异步任务接口**（POST /fba/tasks 返回 task_id +
  GET /fba/tasks/{id} 轮询进度/结果，复用 _run_serial 串行锁；同步 1200s 模型撑不住全流程），
  并补**结构化结果提取**：Shipment ID(FBAxxxx)/FC/每票箱数/placement fee（现散在页面 dump 和
  s4c_results.json，无结构化）→ 中转站回写货件表。
- 已内置且必须保留的护栏：绝不点 Accept charges/最终 Submit/填 PRO；废 wf 待人工 void；
  自动选 placement fee 最低方案（**注意：这与 API 通道"等货代报价人工拍板"口径不同**，需拍板统一）。
- 产出的询价单 `{store}_split_quote_auto.md/.json` 可直接喂货代沟通模块（AGENT_FORWARDER）。
- 部署约束：封装仅 Mac；紫鸟 webdriver 模式独占整机；UI 选择器脆弱（英文文案正则，改版即断）；
  仓库无 LICENSE（源自紫鸟官方 demo），商用前确认授权。

## 8.7 去飞书化清单（已核实全部 20 个卡片动作 + 6 类消息意图的依赖面）

**A. 必须先补 REST/下沉（删飞书的前置）**
1. 建仓后半段：confirm-placement / labels（= v1 改造①，live 按次传参替代 INBOUND_LIVE_SUBMIT——
   该环境变量共 4 处门控全在 feishu/service.py，进程级开关在多运营 LAN 下是事故源）。
2. weekly 整包三件套（周汇总/整包询价群发/整包比价）：逻辑只在 app/feishu/service.py，
   先下沉 services 再端点化（GET /api/weekly/summary 等）——或按新架构退化为"运营各自按批次询价+
   服务端定时周汇总"（待拍板）。
3. 一键采购链（工厂确认→采购单→落批次→建仓）：各步 REST 均已存在，只缺编排——**建议不补
   服务端编排端点，改由运营对话按序调用**（贴合新架构，失败停步天然由对话承接）。
4. 运营身份：/api/feishu/operators → 渠道中立 /api/operators（§8.5）。
5. 回价通知：飞书私信 `_notify_operator_quote` → 运营对话轮询 `GET /api/inquiries/messages/pending`
   （端点已存在可扩展）；"全报齐自动推比价"触发逻辑需重设计或降级为轮询时提示。
6. **状态迁移必须随下沉搬走**：'询价中'（service.py:703）、'运输已配置'（service.py:962）目前
   只写在飞书动作里，漏搬=状态机静默断裂+整包询价重复发。

**B. REST 补齐后可直删**：feishu_client.py、feishu/handlers.py（长连接/去重）、cards.py、
inquiry_port.py、routers/feishu.py 的 ws/simulate 端点、FeishuSession 表、output/_feishu/、
tests/test_feishu_*、lark-oapi 依赖、.env FEISHU_*。
注意 main.py:9 是硬 import（feishu_models），Operator 表被 inquiry_service/store_profile_service
复用——先完成 A-4 迁移再删。

**C. 保留改造**：workspace.py（→ workspace_service，见 §8.5）；Operator 管辖模型。

## 8.8 改造清单 v2（合并 v1，按依赖排序）

| # | 侧 | 改造 | 备注 |
|---|---|---|---|
| ⓪ | 中转站 | 鉴权中间件：X-API-Key 分级（agent 白名单/admin），key→operator，localhost 豁免保前端；服务端 403 一切 DELETE+cancel | 红线从文本约束升级为服务端强制，任何 agent（含 Codex）绕不过 |
| ① | 中转站 | POST /api/batches/{id}/confirm-placement、/labels，live 按次传参 | v1 改造①；去飞书 A-1 |
| ② | 中转站 | 预检路由挂 check_store/check_all + fba.ping | v1 改造② |
| ③ | 中转站 | purchase_plan_service._COUNTRY 补全站点映射 | v1 改造③ |
| ④ | 中转站 | build 失败草稿 planId 透出 + 短路返回统一形状 | v1 改造④ |
| ⑤ | 中转站 | Brand.inbound_channel + Batch.channel/operator 列；用 **main.py 版 _ensure_columns** 补列（create_all 不加列；database.py 版整体吞异常勿用）；初始化 UPDATE 通道值 | §8.2 |
| ⑥ | 中转站 | POST /api/inbound/import-excel（Excel→批次） | §8.3 来源B |
| ⑦ | RPA仓库 | fba_pipeline 封异步任务 HTTP + Shipment ID/FC/fee 结构化提取 | §8.6；需拍板由谁维护 Mac 服务 |
| ⑧ | 中转站 | 去飞书：weekly 下沉端点化、operators 中立化、状态迁移下沉、通知轮询、workspace_service 平移；然后删 B 清单 | §8.7，可分批 |
| ⑨ | 中转站 | RPA 回流批次配对（excel-{hash} vs offline-{hash}） | §8.3 |
| ⑩ | 中转站 | Batch.status 白名单校验（PUT 现无校验，String 自由文本；双通道+多运营打点必发散） | 统一实际值序列：数据准备→已建仓→运输已配置→(赛狐回流)已同步 |

## 8.9 待拍板 v2（新增，v1 §7 仍有效）

| # | 问题 | 背景/推荐 |
|---|---|---|
| 7 | XINGNEST 定位 | 是 XING NEST CO., LIMITED 名下新店还是舟峰/保峰换壳？建 Brand 绑哪个 Company/Factory？报关 company.id in (8,9) 硬编码要不要顺势去硬编码化 |
| 8 | SE/BF 紫鸟接入 | 需先在紫鸟添加店铺+登录+2FA 配置，各人工陪跑一次回填店铺差异记录；是否配紫鸟"自动二步验证"实现无人值守 |
| 9 | RPA 完成口径 | 停 Step3（箱标后）还是到 Step4 tracking 页前？（运营 07-18 指令 Step3，07-22 起 Xingnest 一条龙 Step4）建议统一 Step4 并允许按店覆盖 |
| 10 | 方案选择口径统一 | RPA 线现自动选 placement fee 最低；API 线口径是等货代报价人工拍板。建议 RPA 线也改为"解析方案→报告→运营确认后再选"，与门 B 对齐 |
| 11 | 改造⑦归属 | RPA 仓库侧实现（推荐，靠近紫鸟）；那台 Mac 的固定 IP/常驻性/与 A+ 抢锁问题；是否需要第二台紫鸟机 |
| 12 | 运营身份形态 | 每运营一把 API key（推荐，随改造⓪一起发）vs X-Operator 头过渡 |
| 13 | weekly 整包流程去向 | 保留端点化 vs 退化为"按批次询价+服务端定时周汇总"（推荐后者，贴合对话架构） |
| 14 | Excel 表格约定 | 是否保证单品牌单站点一张表？混表按什么列拆分 |
| 15 | HU 的双轨期 | HU 切 RPA 后 amazon_store='qifengz' 是否保留（API 线备用/EU 逻辑对 RPA 无意义），防误走 API 线 |
