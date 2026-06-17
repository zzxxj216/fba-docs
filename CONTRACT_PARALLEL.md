# CONTRACT — 询价线 / 飞书线 并行开发接缝（两条线互不干扰）

> 在 `D:\amazon`（FastAPI + MySQL 的 FBA 系统）里并行开发两条线。
> **询价线(Track-A)= 能力层（企微渠道）**；**飞书线(Track-B)= 界面层（内部运营入口）**。
> 每条线在独立 git worktree 里开发，最后由集成者合并 + 跑全量测试。

## 0. 环境（每条线 worktree 第一步）

- 把主仓的 `.env` 拷进本 worktree 根目录：`cp /d/amazon/.env .`（`.env` 已 gitignore，worktree 没有）。
  里面已备好凭据：`LLM_*`(jiekou.ai)、`QIWE_*`(企微)、`FEISHU_*`(飞书)、`DB_URL`(MySQL)。
- Python 直接用系统解释器（`python`）。需要的额外包自己 `pip install`（如 Track-B 的 `lark-oapi`）。
- MySQL 库 `fba_docs` 是共享的：**测试不要污染它**——测试用 SQLite 或独立 schema，事务回滚。

## 1. 文件归属（严格，别碰对方的文件）

**Track-A（询价）拥有：**
- `app/models.py`（加 `QuoteLine` 表；给 `Inquiry` 加 `ref_code`/`lanes_snapshot`；给 `ForwarderMessage` 加 `attribution_status`/`reply_to_msg_id`）
- `app/services/inquiry_service.py`（新）、`app/services/forwarder_service.py`（已有，可改）
- `app/modules/quote_extractor.py`、`app/modules/quote_extractor_llm.py`（新）
- `app/routers/inquiry.py`（新）
- `tests/test_inquiry_*.py`、`tests/test_quote_*.py`

**Track-B（飞书）拥有：**
- `app/feishu_client.py`（新，lark-oapi）
- `app/feishu/`（新包：`handlers.py`/`cards.py`/`intake.py`/`service.py`）
- `app/feishu_models.py`（新；若需 `Operator`/`Session` 模型放这里，**勿动 `app/models.py`**）
- `app/routers/feishu.py`（新）
- `tests/test_feishu_*.py`

**共享（各自在自己 worktree 里加一行注册自己的 router；合并时冲突极小）：**
- `app/main.py` — `from .routers import ...` + `app.include_router(...)`

## 2. 接缝：飞书怎么调询价（飞书不碰询价内部逻辑）

Track-A 实现并暴露下列 `inquiry_service` 函数；Track-B 按**这些签名**对接（未就绪时本地 stub）：

```python
start_inquiry(db, batch_id) -> Inquiry          # 建询价 + LLM 起草正文（待人工确认）
send_inquiry(db, inquiry_id) -> dict            # 群发各货代（企微 qiwe）
get_comparison(db, inquiry_id) -> dict          # 各货代整包总价 + 推荐（带理由/风险/缺仓）
choose_forwarder(db, inquiry_id, quote_id)      # 人工选定
```
`app/routers/inquiry.py` 暴露同样动作的 REST，飞书卡片按钮走 REST 即可。

## 3. 技术约定（两条线都遵守）

- **LLM**：用 `app/llm_client.py`（`chat()` / `chat_json()`），已接 jiekou.ai（`claude-opus-4-8`）。**别自己拼网络请求。** 缺 key 时它抛 `LLMUnavailable`，要优雅降级。
- **企微**：用 `app/qiwe_client.py`（`send_text` / `list_rooms`），已验收。
- **飞书**：`lark-oapi` 长连接（`lark.ws.Client`），免公网。`app_id/secret` 在 `.env` `FEISHU_*`。
- **DB**：`app/database.py`（MySQL）。新表挂到 models 的 `Base`，启动 `create_all` 自动建。
- **设计蓝本**：询价看 `AGENT_FORWARDER.md`（v2，含报全校验/图片提取/归属隔离/整包比价）。

## 4. 安全红线（询价线务必内建）

系统**永不**：确认发货 / 确认付款 / 承诺固定货量或长期合作 / 对货代说"别人更便宜""做到这个价就给你下单" / 未经人工确认发最终确认。询价类消息可自动发；议价/选定走人工确认。

## 5. 交付标准

每条线：功能完成 + **pytest 测试覆盖多场景**（询价尤其要：裸价/多单位/部分报价+追问/图片报价/归属/整包比价）+ 自测通过 + 在 worktree 里 commit。完成后报告：做了什么、文件清单、测试结果。
