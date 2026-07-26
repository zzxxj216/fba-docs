# 建仓 Skill 规划（草案 v1，2026-07-26）

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
| 4 | 补仓 Excel 入口 | phase 2（需先打通 Excel→批次线，当前只有遗留向导线支持） |
| 5 | 向导线 /api/inbound/* 去留 | 标记遗留；skill 不用，前端暂保留 |
| 6 | 分仓确认是否必须等货代报价 | 按现有口径：报告方案后停住，等报价+用户拍板；即使只有 1 个方案也不自动确认 |
