# 开源 + 运营本地实例 改造方案（2026-07-27）

> 目标架构（用户 2026-07-27 拍板方向）：**项目开源，运营各自 git clone 到本地电脑运行**，
> Codex 按仓库文档驱动本地 API；**mcapi（已部署在局域网公用服务器）是唯一密钥持有层**；
> **业务数据/日志进服务器中央 MySQL**；**生成文件落运营本地 output/**；运营端 .env 不含密钥。
> 本文取代 SKILL_INBOUND.md §8 的"中转站"模型（fba-docs 不再需要中央部署的前后端）。

```
运营电脑A/B/C：git clone fba-docs → 本地 uvicorn:8000 → Codex 按 AGENTS.md 驱动
      │  output/ 生成文件落本地
      ├──HTTP──► 公用服务器 mcapi:8100（SP-API + 赛狐 + LLM 密钥全在这里；X-API-Key 每运营一把）
      └──MySQL──► 公用服务器中央库 fba_docs（低权限 DML 账号；schema 变更只在服务器做）
服务器另跑一份 fba-docs "通道 worker" 实例：企微 webhook / （过渡期）飞书 WS 单点消费
```

## 0. 调研结论速览（2026-07-27 四路并查）

**利好**
- **git 历史无密钥泄露**：138 个提交全扫，.env 从未提交，所有凭据只以变量名出现。
- **mcapi 赛狐通道已存在**：`platforms/sellfox/client.py` 完整实现 token 缓存+HMAC 签名+
  40001/40019 重试；`POST /api/v1/sellfox/call` 任意 path+body 透传已可用；凭据已在服务器。
  → 缺的只有主动 1.1s 限流（现在是被动退避）。
- **fba-docs 无绕行直连**：全部赛狐调用都过 `sellfox_client.call()` 一个咽喉（5 个 service、
  约 24 处调用点零改动），代理化只改一个函数。
- 除 DB_URL 外所有密钥模块（llm/qiwe/feishu/ai_mapping）都已优雅降级，缺 key 不崩进程。
- mcapi 有现成 docker-compose（api+MySQL8+alembic），中央 fba_docs 库可同实例不同库。

**硬约束**
- **现仓库不可直接公开**：自首个提交起，真实公司档案（四主体全称+统一社会信用代码+中英文
  地址+手机号）、**报关申报价换算公式（出口申报合规风险，性质最重）**、托书单价固定写 2、
  两级合同加价链、货代客户编号、真实 FBA 货件号、供应商/仓库 ID 就在 doc_rules/验收报告/
  临时脚本/rule_engine 默认值/**提交信息本身**里。历史重写不可行 → 若公开必须**新仓首发拆分**。
- **tools/tunnel_keepalive.py 硬编码子域名 fbadocs-zane**：可推导出直达本机 8000 无鉴权 API
  的公网 URL——无论是否开源都应立即作废。
- **mcapi 零鉴权 + CORS 全开**，且带取消入库计划等危险接口和两个万能透传口——
  放到局域网前，鉴权+危险接口拦截是硬前置。
- **多实例+中央库的并发风险是真事故级**：build 的幂等护栏是 check-then-act（commit 在数分钟
  外呼之后），双实例同时点建仓会创建两个真实亚马逊入库计划（红线不能自动取消）；
  生成采购单同理会出两张真实赛狐采购单。
- 运营端启动即崩点：main.py `_ensure_columns` 无 try/except，DB 账号收走 DDL 权限后启动直接崩。
- 新 clone 生成能力为零：templates_store/ 被 gitignore，模板原件不随仓库走。
- GeneratedDoc.path 存本机绝对路径：A 生成的文件 B 下载 404、**zip 静默缺文件**（比报错更危险）。

## 1. Phase 0——立即做（与开源无关的安全项）

| # | 事项 | 位置 | 量 |
|---|---|---|---|
| 0.1 | 作废 fbadocs-zane 隧道子域名；tunnel_keepalive.py 局域网化后整删 | tools/ | 即刻 |
| 0.2 | mcapi 加 X-API-Key 鉴权：config 加 key→运营名映射 + core/auth.py 依赖 + router 一行挂载；豁免清单（/health、飞书/tiktok/etsy/shopify 回调） | mcapi 仓 | 0.5-1天 |
| 0.3 | mcapi 危险接口 403 拦截：DENY 表（amazon_fba cancel、amazon DELETE listings、shopify cancel/refund/DELETE×5、tiktok cancel/DELETE、etsy DELETE、ads kill-switch），非 admin key 一律 403 | mcapi 仓 | 0.5天 |
| 0.4 | 两个透传口（/sellfox/call、/etsy/call）handler 内按 path 拦截 cancel/void/delete/reject 类关键词（更稳妥：白名单只放行 fba-docs 实际用的 14 个赛狐 path） | mcapi 仓 | 0.25天 |

红线技术化：这是把"Claude/Codex 无删除取消权限"从文本约定升级为服务端强制的机会——
运营 key 永远调不到 cancel 类接口，谁的 Codex 都绕不过。

## 2. Phase 1——密钥收归服务器

| # | 事项 | 量 |
|---|---|---|
| 1.1 | mcapi sellfox 通道补主动限流：asyncio.Lock+1.1s 最小间隔（移植 fba-docs sellfox_client 逻辑的 async 版，~15 行）。顺带解决 N 实例限流分裂和 token 互踢 | 0.25天 |
| 1.2 | fba-docs `sellfox_client.call()` 改为 HTTP 调 `{MCAPI_BASE}/api/v1/sellfox/call`（带 X-API-Key），保留签名逻辑做 `SELLFOX_MODE=direct` 双模式（服务器侧 worker 实例复用同一代码库）；上层 24 处调用零改动 | 0.25天 |
| 1.3 | LLM 代理：mcapi 加 `POST /api/v1/llm/chat`（OpenAI 兼容透传 jiekou.ai，复用其 AsyncOpenAI 模式）；fba-docs llm_client 指向 mcapi。ai_mapping 从 Anthropic 直连并入 llm_client 网关（消灭 ANTHROPIC_API_KEY 键）。若拍板运营不用图片报价提取，可降级省掉 | 0.5+0.5天 |
| 1.4 | 通道 worker 固定服务器：fba-docs 加 `CHANNEL_WORKER=1` 开关（企微 webhook 处理/飞书 WS 只在服务器实例启用）；qiweapi 回调 URL 指服务器；切换用 sync_msgs 补捞防丢消息 | 0.5天 |
| 1.5 | 运营端 .env 定稿（.env.example 进仓库）：`MCAPI_BASE`、`MCAPI_KEY`（每运营一把，身份即凭证，可吊销）、`DB_URL`（低权限账号）、`OPERATOR_NAME`。其余键全删 | 0.5天 |

## 3. Phase 2——中央库 + 多实例适配

| # | 事项 | 量 |
|---|---|---|
| 2.1 | DDL 出运营端：create_all/_ensure_columns/_ensure_database 加"无 DDL 权限跳过"门控；schema 变更改为服务器上管理员执行的 migration 脚本；运营 MySQL 账号只授 DML | 0.5-1天 |
| 2.2 | **关键写入原子占坑（防真实重复事故，最高优先）**：build_for_batch 入口条件 UPDATE 占坑（`SET inbound_plan_id='PENDING-{uuid}' WHERE id=? AND inbound_plan_id IS NULL`，rowcount=1 才继续）；create_purchase_order 同法；RuleConfig 加 unique(key,scope)；IntegrityError 转 409"他人正在操作" | 1天 |
| 2.3 | 缩短持锁：build 在算完报关价后先 commit 再做亚马逊外呼（现在行锁持数分钟） | 0.5天 |
| 2.4 | 归属模型：请求头/OPERATOR_NAME → Batch.owner；写操作限 owner；DELETE 类端点仅 admin（复用 Operator.scope_brand_ids 雏形可后置） | 1-2天 |
| 2.5 | 文件路径适配：GeneratedDoc.path 相对化 + owner 字段；跨机查看提示"文件在 X 的电脑"而非 404；zip 缺文件显式警告不静默；生成记录同键去重（防 docs 死链堆积） | 1天 |
| 2.6 | 模板中心化：推荐 Template 加 BLOB/服务器文件接口，本地 templates_store 作缓存按需拉取；过渡方案=公用模板进仓库改相对路径 | 1-2天(过渡0.5) |
| 2.7 | contract_service.py 硬编码 D:\amazon 路径改用 database.OUTPUT_DIR/TEMPLATE_STORE（不做则非 D:\amazon 部署采购合同全挂） | 10分钟 |
| 2.8 | 顺带修：delete_batch 目录名与 -MMDD 新规则不一致；database.py 去 root:123456 默认 DSN（缺配置即报错） | 0.5天 |

## 4. Phase 3——开源发布（先拍板 §6.1）

**路线 A（受众=内部运营，推荐）**：GitHub 私有 org 仓 + 成员授权。脱敏压力大减，
只需处理：.env.example、隧道子域名、CLAUDE.md 个人路径段、临时脚本清理。1 天内完成。

**路线 B（真公开）**：**新仓首发拆分**（现仓历史不可洗）：
- 开源引擎仓（全新 git init，无血缘）：app/（去业务化后）、static/、tests/、requirements、
  README/.env.example/AGENTS.md、脱敏版 DESIGN（保留四层架构/字段字典/RuleConfig 机制，
  删除一切公司名与价格系数）。
- 永不入开源仓：docs/doc_rules（**报关价公式类连"脱敏版"都不留**）、验收报告×3、
  _accept2/_smoke/_e2e/_p2 脚本、ScreenShot 截图、PURCHASE_ORDER_API.md（供应商/仓库 ID）。
- 代码去业务化清单：rule_engine DEFAULT_RULES 境外收货人真实值→占位（真实值走私有 seed）；
  field_registry company.id==7/in(8,9) 硬编码→Company 布尔字段；app.js 工厂名硬编码→读接口；
  seed.py F:\聊天记录 路径→环境变量；品牌缩写/测试样例是否虚构化（待拍板）。
- 现仓转私有归档，继续作为内部历史；私有业务配置（doc_rules/档案 seed/模板）随服务器分发，
  运营端 Codex 需要业务规则时经服务器 API/私有渠道下发，不回流开源仓。

**AGENTS.md（两条路线都要做）**：Codex 的驱动文档（替代已删的四个 skill）——
本地 API 基址与启动、四条业务线 SOP（采购计划/建仓/询价/文件生成，含三道确认门与失败处置表）、
**红线清单**（一切 DELETE/cancel 禁调，需要清理只报告）、多实例注意（别动别人的批次）。
约 1 天（素材=git 历史 035ae1d 里四个 SKILL.md 的内容改写）。

## 5. Phase 4——服务器部署

- mcapi 用现成 docker-compose 上服务器（.env 卷挂载可写——etsy/tiktok token 回写机制需要）；
  或 Windows 裸跑+进程守护（nssm），端口固定 8100（现场日志曾 8000/8001 漂移，要统一）。
- 中央 MySQL：同实例新建 fba_docs 库；管理员账号跑首次 migration+seed（真实档案数据在服务器
  侧初始化，运营端不需要 F:\聊天记录 的 xlsx）；运营 DML 账号；连接数规划（N 实例×15）。
- 服务器另跑一份 fba-docs worker 实例（CHANNEL_WORKER=1 + 全量密钥），处理企微回调与
  （过渡期）飞书；停掉历史上 Windows/Mac 双机实例的同步任务（有双跑合库教训）。

## 6. 待拍板

| # | 问题 | 建议 |
|---|---|---|
| 6.1 | **开源=公开 GitHub 还是内部私有分发？** | 若受众只是运营，强烈建议路线 A（私有仓+授权）：报关公式合规风险、品牌可反推经营主体，公开收益存疑。真要公开走路线 B 新仓拆分 |
| 6.2 | MCAPI_KEY 算不算"运营端不放密钥"的例外 | 建议接受：它是内网身份凭证（可吊销、只标识运营），不是外部服务密钥 |
| 6.3 | DB 直连中央库 vs 业务数据也 API 化 | 先直连+低权限账号（改造小）；API 化工作量大可后置 |
| 6.4 | 生成文件坚持落运营本地？ | 按拍板执行（owner 标记+zip 警告兜底）；提醒代价：互看文件不便、zip 可能不全。若终点都是"发给货代"，服务器共享目录其实更省事 |
| 6.5 | 询价线（企微发送）开放给运营吗 | 开放则 worker 实例代理发送动作；不开放则运营端自动降级只读比价 |
| 6.6 | 危险接口权限模型 | operator key 全禁 cancel/delete（红线技术化）；admin key 留给用户本人；采购单作废等用户手动流程走前端/赛狐后台，不经运营 key |
| 6.7 | 赛狐限流口径 | 全局 1.1s 串行先上（保守），高峰排队明显再谈按接口分桶 |
| 6.8 | 运营端图片报价提取要不要 | 要则 LLM 代理必做（无正则兜底）；不要则文档明示"发截图没反应"是降级不是 bug |

## 7. 实施顺序建议

```
Phase 0（安全硬前置，~2天）→ Phase 1（密钥上收，~2天）→ Phase 2（多实例适配，~4-6天）
→ 内测：两台运营机 + 服务器并行跑一周 → Phase 3（开源发布，路线A ~1天 / 路线B ~2-3天）
→ Phase 4 与 0-2 并行推进部署
```
总量约 9-14 个工作日。其中 2.2（原子占坑）在任何多人使用发生前必须完成——
双实例重复建仓/重复下采购单是真金白银级事故且触红线（不能自动取消清理）。
