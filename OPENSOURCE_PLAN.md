# 内部分发 + 运营本地实例 改造方案 v2（2026-07-27）

> v1 同日作废段落已删。**v2 已拍板决策**（用户 2026-07-27）：
> ① 内部私有分发（不公开 GitHub，私有仓+成员授权）；② MCAPI_KEY 每运营一把可接受；
> ③ 生成文件落运营本地；④ **敏感业务数据（主体档案/报关价换算系数/货代客户编号等）
> 保存在运营本地，不上中央服务器**——中央服务器只存：外部服务密钥、操作日志、
> 关键节点信息（可追溯）。

## 1. 目标架构（数据本地化版）

```
运营电脑A/B/C（git clone 私有仓 fba-docs）
  ├─ 本地实例 uvicorn:8000 ← Codex 按 AGENTS.md 驱动
  ├─ 本地业务库（SQLite 默认，免装 MySQL；批次/货件/产品/主体档案/RuleConfig/模板/生成记录 全在本地）
  ├─ 本地初始化包（离线分发：档案 seed+规则真实值+doc_rules 文档+模板原件）
  └─ output/ 生成文件（本地）
        │  HTTP + X-API-Key（每运营一把）
        ▼
公用服务器 mcapi:8100（唯一密钥层+协调层）
  ├─ 外部服务密钥：SP-API（已有）/ 赛狐（已有，/api/v1/sellfox/call 现成）/ LLM
  ├─ 操作日志（审计：谁·何时·调了什么写操作）
  ├─ 关键节点登记（可追溯 + 防重占坑：建仓/采购单/确认分仓/询价发送 的唯一登记）
  └─ 通道 worker（企微 webhook 收报价→消息暂存中继）
```

**数据分层原则**：
- **只在本地**：主体真实档案（信用代码/地址/手机号）、报关价换算系数（RuleConfig 真实值）、
  货代客户编号、模板原件、全部业务明细（批次/货件/SKU/价格）、生成文件。
- **只在服务器**：外部服务密钥；操作日志；关键节点（只带操作标识号——planGroupNo/
  inbound_plan_id/purchase_no/FBA号/文件名清单——不带价格/档案等敏感 payload）。
- 敏感种子**不进 GitHub**（私有仓也不进）：走"本地初始化包"离线分发（内网共享/一次性拷贝），
  一条命令导入本地库。doc_rules 四文档随包走，从仓库移除。

## 2. 相比 v1 的重大简化（业务库本地化的红利）

| v1 问题 | v2 结果 |
|---|---|
| 中央 MySQL 账号/DDL 收权/启动崩点 | **消失**——本地 SQLite，create_all 自建全列（dialect 门控已存在，tests 即 SQLite，零改动） |
| 多实例并发写同库（行锁/丢更新） | **消失**——各自本地库 |
| GeneratedDoc.path 跨机 404 / zip 静默缺文件 | **消失**——文件与记录同机 |
| 模板中心化（BLOB/文件服务） | **不需要**——模板原件随初始化包到本地 |
| 归属模型（owner 字段+权限） | **降级**——本地库天然隔离；服务器节点登记自带 operator |
| 双运营重复建仓/重复下单（原方案中央库原子占坑） | **改为服务器"关键节点占坑"**（§4.2）——本地库隔离反而让重复风险更隐蔽，必须由服务器唯一登记拦截 |

## 3. Phase 0——安全硬前置（不变，先做）

| # | 事项 | 量 |
|---|---|---|
| 0.1 | 作废 fbadocs-zane 隧道子域名；tunnel_keepalive.py 整删 | 即刻 |
| 0.2 | mcapi X-API-Key 鉴权（key→运营名，ContextVar 仿 store_context 模式；回调/OAuth/health 豁免清单） | 0.5-1天 |
| 0.3 | mcapi 危险接口 403（DENY 表：amazon_fba cancel、各平台 DELETE/cancel/refund；operator key 全禁，admin key 留用户本人） | 0.5天 |
| 0.4 | 透传口管控：/sellfox/call 改**白名单**只放行 fba-docs 实际用的 14 个 path（cancel.json 单独 admin+审计）；/etsy/call 同理 | 0.25天 |

红线技术化：运营 key 在服务端就调不到任何取消/删除接口，Codex 想绕也绕不过。

## 4. Phase 1——密钥上收 + 协调层（服务器侧）

| # | 事项 | 量 |
|---|---|---|
| 1.1 | mcapi sellfox 通道补主动限流（asyncio 版 1.1s 最小间隔，~15 行）；token 单点缓存顺带解决多实例互踢 | 0.25天 |
| 1.2 | fba-docs `sellfox_client.call()` → HTTP 调 `{MCAPI_BASE}/api/v1/sellfox/call`（带 key）；保留 `SELLFOX_MODE=direct` 给服务器 worker 复用；上层 24 处调用零改动 | 0.25天 |
| 1.3 | mcapi 审计日志：鉴权依赖里对全部写方法自动落库（operator/method/path/时间/摘要）；另开 `POST /api/v1/audit/log` 供本地实例上报纯本地动作（生成文件/校验） | 0.5天 |
| 1.4 | **关键节点登记 API**（防重+追溯核心）：`POST /api/v1/checkpoints` body={scope_key, node, operator, refs}，scope_key 唯一约束（如 `PPG2607240001:build`、`PPG2607240001:po`）——已存在且非本人 → 409 返回先占者信息；`GET /api/v1/checkpoints?scope=` 查追溯链。存 mcapi 自己的 MySQL | 0.5-1天 |
| 1.5 | fba-docs 在四个关键写入点接入 claim：build_for_batch 前、create_purchase_order(dry_run=false) 前、confirm-placement(live) 前、询价 send 前——先 POST checkpoint 占坑，409 则中止并提示"运营X已在Y时间操作过"；成功后回填 refs（inbound_plan_id/purchase_no/FBA号）。服务器不可达时的策略：**默认拒绝执行写操作**（fail-closed，防脱网双开） | 1天 |
| 1.6 | ~~LLM 代理~~ **取消**（拍板 2026-07-27）：报价提取/归属判别/AI 映射全部由运营端 **Codex 承担**（它就是 LLM）。连锁红利：服务器也不再需要 LLM——ANTHROPIC_API_KEY/LLM_* 三键整体退役，jiekou.ai 账号可停；llm_client/quote_extractor_llm/ai_mapping 保留优雅降级不删 | 0 |
| 1.7 | **mcapi 消息中继**（取代"fba-docs 通道 worker"，飞书已在删除清单，worker 实例不再需要）：① mcapi 加 `POST /api/qiwe/callback` 存中继表（qiweapi 回调地址本就指向该服务器——mca_run.log 里持续 404 佐证，加路由即通）；② `POST /api/v1/qiwe/send` 发送代理（QIWE_TOKEN 只在服务器）；③ `GET /api/v1/qiwe/relay/messages?room_ids=&since=` 运营端拉取；④ 切换期用 sync_msgs 补拉兜底（游标式全量，服务器侧跑） | 1-1.5天 |

**运营端 .env 定稿（.env.example 进仓库）**：
```
MCAPI_BASE=http://<服务器IP>:8100
MCAPI_KEY=<每人一把>
OPERATOR_NAME=<运营名>
# DB_URL 不配 = 默认本地 SQLite ./fba_docs.db；老机器可继续配本地 MySQL
```

## 4.5 询价线 Codex 化（拍板：开放给运营，无 LLM 代理）

新分工：**服务器只做哑管道**（收/存/转发消息，不做任何智能处理），**Codex 是提取与归属的
大脑**，fba-docs 是纯记账。

| # | 事项 | 量 |
|---|---|---|
| I.1 | fba-docs 新增**结构化报价提交端点** `POST /api/inquiries/{id}/quotes`：body={forwarder_id, currency, customs_fee_monthly?, lines:[{fc,price,unit,channel?,eta_days?,...}], raw_ref}——复用现成 `_persist_quote` 按 FC upsert；Codex 读原文/看图提取后直接提交，绕过内置 LLM | 0.5天 |
| I.2 | fba-docs qiwe_client 改调 mcapi 代理（发送）；新增中继拉取导入：把 relay 消息落本地 ForwarderMessage 走现有归属管道（规则归属保留：引用/单开放询价/ref_code；LLM 归属降级由 Codex 人工 assign 接手） | 0.5-1天 |
| I.3 | **图片链路四缺口**（2026-07-27 核实：现状图片消息在入口被整条丢弃，从未落库）：① forwarder_service 无正文事件也落 raw（最致命，改 1 行判断）；② **真实样本探针**——改完①让货代发一张真图，看 raw 里是 base64RawData 还是要另查 qiweapi 媒体下载接口（qiwe_client 现无此封装，method 名未知），**方案在样本后才能定**；③ 图片落地目录约定+写 media JSON+消息流水接口带 media；④ msg_type 数字码归一成 text/image/file | ①10分钟+②探针+③④0.5-1天 |
| I.4 | AGENTS.md 询价章：Codex 提取**必须带确认门**——向运营复述"原文/图 + 提取出的结构化行"，确认后才 POST /quotes（提错价→比价错→选错货代，责任已从系统 LLM 转移到 Codex）；归属歧义（同群多品牌）走 pending+人工指认，Codex 不猜 | 并入 2.5 |

## 5. Phase 2——本地化适配（fba-docs 侧）

| # | 事项 | 量 |
|---|---|---|
| 2.1 | DB_URL 默认值改 `sqlite:///fba_docs.db`（去掉 root:123456 默认 DSN）；验证全链路 SQLite 回归（tests 已是 SQLite，重点回归 ilike/日期函数/并发写） | 0.5天 |
| 2.2 | 本地初始化包：`tools/build_seed_pkg.py`（管理员在本机导出：companies/factories/brands/products 档案 + RuleConfig 真实值 + doc_rules/ + templates_store/ → 一个 zip）+ `POST /api/seed/import-pkg`（运营端导入）；seed.py 去 F:\聊天记录 硬编码 | 1天 |
| 2.3 | contract_service.py 硬编码 D:\amazon 路径改 database.OUTPUT_DIR/TEMPLATE_STORE | 10分钟 |
| 2.4 | 仓库卫生（私有仓，历史保留可接受）：HEAD 移除 doc_rules/（→初始化包）、验收报告×3、_accept2/_smoke/_e2e/_p2 脚本、ScreenShot 截图、PURCHASE_ORDER_API.md 供应商ID段（→初始化包）、CLAUDE.md 个人路径段；补 README（clone→pip install→配 .env 三行→导入初始化包→start.bat）| 0.5-1天 |
| 2.5 | AGENTS.md（Codex 驱动文档，替代已删 skills）：本地 API 用法 + 四条业务线 SOP（采购计划/建仓/询价/文件生成，三道确认门+失败处置表，素材=git 035ae1d 的四个 SKILL.md）+ **红线清单** + 关键节点占坑语义（409=别人做过了，停下问用户）。公式/档案细节不写入，引用本地 doc_rules | 1天 |
| 2.6 | 顺带修：delete_batch 目录名 -MMDD 不一致；MCAPI_BASE 无默认值化（缺失即报错，防连错本机） | 0.5天 |

## 6. Phase 3——分发与部署

- 私有仓授权运营 clone（GitHub collaborator）；不新建仓、不洗历史（内部可见可接受）。
- mcapi 上公用服务器：docker-compose（.env 卷挂载可写——token 回写需要）或 Windows 裸跑+nssm；
  端口固定 8100；停掉历史双机实例的同步任务。
- 初始化包经内网共享/一次性拷贝分发给每个运营；换系数/换档案时重新出包（RuleConfig 也可
  运营本地自改，包只是基线）。
- 每运营发 MCAPI_KEY；服务器 worker 实例配全量密钥。
- 内测：两台运营机 + 服务器跑一周（重点验证：占坑 409 拦截双开建仓、赛狐代理限流、
  webhook 中继不丢消息）。

## 7. 追溯链设计（中央服务器记什么）

| 类型 | 内容 | 来源 |
|---|---|---|
| 审计日志 | operator / 时间 / method+path / 结果摘要 | mcapi 网关自动 + 本地实例上报 |
| 关键节点 | `PPG…:confirm`(工厂确认)、`PPG…:build`(建仓,ref=inbound_plan_id)、`PPG…:po`(采购单,ref=purchase_no)、`batch…:placement`(确认分仓,ref=FBA号列表)、`inq…:send`(询价外发)、`batch…:docs`(文件生成,ref=文件名清单) | fba-docs 关键写入点 claim+回填 |
| 消息中继 | 货代企微回复原文暂存（供运营端拉取，拉取后可清理） | 服务器 worker |

节点 payload 只有标识号与时间，不含价格/档案/公式——满足"可追溯"且不违反数据本地化。

## 8. 剩余待拍板

| # | 问题 | 建议 |
|---|---|---|
| 8.1 | ~~询价线开放~~ **已拍板：开放 + 无 LLM 代理（Codex 承担提取）**，见 §4.5 | — |
| 8.2 | 占坑 fail-closed 策略确认 | 服务器不可达时禁止建仓/下单类写操作（防脱网双开）；紧急时管理员可在本地实例设 `CHECKPOINT_BYPASS=1` 兜底 |
| 8.3 | 初始化包分发形式 | 内网共享目录（推荐）/U盘；是否加密看内网信任度 |
| 8.4 | 老机器（用户本机 MySQL 数据）迁移 | 用户本机保持 MySQL 不动（DB_URL 显式配）；或导出进初始化包给新机 |
| 8.5 | 中继消息保留策略 | 货代回复原文（含报价）在服务器暂存多久？建议拉取确认后保留 30 天自动清理，拉取动作入审计（追溯靠节点+审计，不靠原文长存） |
| 8.6 | 同群多品牌归属歧义（多运营放大） | 中继按 room 拉，同一条报价可能被两个运营各自认领到不同询价。约定：Codex 只在消息上下文明确时归属，含糊一律 pending 人工指认；长期解=每品牌独立货代群 |
| 8.7 | AI 映射（模板登记）也交 Codex？ | 建议是：Codex 读模板 xlsx 直接产 mapping JSON 调 CRUD 登记，ai_mapping.py 留降级——这样 ANTHROPIC key 彻底退役 |

## 9. 实施顺序与总量

```
Phase 0（~2天）→ Phase 1（~3-4天，含协调层+消息中继）→ §4.5 询价 Codex 化（~2天，图片链路视探针结论）
→ Phase 2（~3-4天）→ Phase 3 部署+内测1周
```
开发总量约 **9-11 个工作日**。
硬顺序约束：0.2/0.3（mcapi 鉴权）先于一切局域网暴露；1.4/1.5（占坑）先于第二个运营接入；
图片链路的 I.3-①（无正文事件落 raw）应尽早合入以便攒真实样本。
