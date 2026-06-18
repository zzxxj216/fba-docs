"""飞书线编排 —— 把"消息/按钮"翻译成动作，调 intake / 询价线接缝，产出回复卡片。

- parse_intent(text)        用 llm_client 理解运营自然语言意图；无 LLM 时关键词兜底。
- handle_message(...)       运营发消息 → 认人 → intake → 待办卡片（或提示）。
- handle_action(...)        卡片按钮回调 → 分发到 intake/询价线 → 结果卡片。

安全红线（CONTRACT §4）：一切"群发/选定/发文件"只在按钮动作里触发，且对应飞书 UI
本就是"人工点击"——本模块不自动替用户点。
"""

import json

from .. import feishu_client as fc
from .. import llm_client
from . import cards, intake
from . import inquiry_port as port
from ..models import Batch, Brand, Inquiry

# 意图集合：restock=看本周补货/待采购（赛狐）；intake=建仓就绪体检/看待办；
# inquiry=发起询价；compare=看比价；help/unknown
INTENTS = ("restock", "intake", "inquiry", "compare", "fill_data", "help", "unknown")

# 主流程 agent 人设：专注亚马逊 FBA 全程管理的内部助手。寒暄/泛问时按它对话（认人+能力+引导）。
AGENT_PERSONA = (
    "你是「亚马逊发货管家」，一个专注【亚马逊 FBA 全程发货管理】的内部助手，服务公司运营。"
    "你的职责是帮运营把一条货从补货管到发货，覆盖：\n"
    "① 看本周补货 / 待采购（从赛狐拉取）\n"
    "② 建仓（创建亚马逊入库计划、拿到分仓目的仓）\n"
    "③ 货代询价、整包比价、协助选货代（企微沟通货代）\n"
    "④ 生成并发送托书\n"
    "⑤ 查批次进度 / 状态\n"
    "说话像个专业干练的同事：中文、简洁、不啰嗦、别堆客套。\n"
    "安全红线：绝不擅自下单/付款/确认成交/群发消息/发文件——这些都要运营在卡片上亲自点确认。\n"
    "当运营只是打招呼或泛泛发问时：先称呼对方名字、点明 ta 管哪些店铺、用一两句说清你能帮的事，"
    "再问 ta 想先做哪件（可提示直接说「看本周补货」「建仓」「查进度」）。不要长篇大论。\n"
    "【重要】你自己不会在后台拉数据或干活：绝不要说「正在拉取」「稍等我去查」「结果出来我会列给你」"
    "这类假装在后台做事的话。真正的动作由系统在运营发出明确指令后执行并直接回结果；"
    "你在对话里只做介绍、确认和引导，不替系统编造进度或结果。"
)

_KEYWORDS = {
    "restock": ["补货", "补仓", "待采购", "采购计划", "补仓计划", "本周补", "要补什么"],
    "intake": ["待办", "体检", "建仓", "就绪", "开始建仓", "我管"],
    "inquiry": ["询价", "报价", "发货代", "找货代"],
    "compare": ["比价", "对比", "哪家", "选货代", "报价对比"],
    "fill_data": ["补数据", "缺数据", "补全", "补资料"],
    "help": ["帮助", "怎么用", "help", "你能做"],
}


def parse_intent(text):
    """理解运营意图。返回 {"intent": str, "batch_hint": str|None}。

    优先 LLM（llm_client.chat_json）；缺 key 或失败时关键词兜底（保证无 LLM 也能走）。
    """
    text = (text or "").strip()
    if not text:
        return {"intent": "intake", "batch_hint": None}
    if llm_client.available():
        try:
            sys = ("你是 FBA 发货系统的内部运营助手意图分类器。把运营的话归为以下意图之一："
                   "restock(看本周补货/待采购/补仓计划——从赛狐拉补货数据)、"
                   "intake(看建仓待办/建仓就绪体检——已有批次能不能建仓)、"
                   "inquiry(发起询价)、compare(看货代比价)、"
                   "fill_data(补全数据)、help(帮助)、unknown。"
                   "注意区分：『看补货/待采购/补仓计划』是 restock；『看待办/建仓体检/开始建仓』是 intake。"
                   "只输出 JSON：{\"intent\": \"...\", \"batch_hint\": \"可选的批次名或编号\"}。")
            res = llm_client.chat_json(
                [{"role": "system", "content": sys},
                 {"role": "user", "content": text}],
                max_tokens=200)
            intent = res.get("intent")
            if intent in INTENTS:
                return {"intent": intent, "batch_hint": res.get("batch_hint")}
        except llm_client.LLMUnavailable:
            pass
        except Exception:
            pass
    return {"intent": _keyword_intent(text), "batch_hint": None}


def _keyword_intent(text):
    low = text.lower()
    for intent, kws in _KEYWORDS.items():
        if any(kw.lower() in low for kw in kws):
            return intent
    return "unknown"           # 认不出 → 走对话（认人+介绍能力），不直接甩卡片


def _safe_send_card(chat_id, card):
    """发卡片；飞书未接通时不崩（返回 None），便于离线/测试。"""
    if not fc.configured():
        return None
    try:
        return fc.send_card(chat_id, card)
    except RuntimeError:
        return None


def _safe_send_text(chat_id, text):
    """发文本；飞书未接通时不崩（返回 None）。"""
    if not fc.configured():
        return None
    try:
        return fc.send_text(chat_id, text)
    except RuntimeError:
        return None


def _managed_shops(db, op):
    """运营管辖的店铺/品牌名（用于对话里点明 ta 管哪些店）。"""
    if op is None:
        return []
    ids = []
    if op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
    names = []
    for bid in ids:
        b = db.get(Brand, bid)
        if b and b.name:
            names.append(b.name)
    return names


def chat_reply(db, op, text):
    """主流程 agent 的文本对话：认人 + 点明管辖店铺 + 介绍能帮的事 + 引导。

    优先 LLM（带人设 + 该运营上下文）；无 LLM 时给确定性的简短介绍。
    """
    shops = _managed_shops(db, op)
    shop_str = ("、".join(shops) if shops
                else ("全部店铺（管理员）" if op and op.is_admin else "（暂未配置管辖店铺）"))
    fallback = (
        f"你好 {op.name or ''}，我是亚马逊发货管家。你负责 {shop_str}。"
        "我能帮你：看本周补货 / 建仓 / 货代询价比价选货代 / 发托书 / 查进度。"
        "想先做哪件？比如直接发「看本周补货」或「建仓」。")
    if not llm_client.available():
        return fallback
    ctx = f"当前运营姓名：{op.name or '（未命名）'}；ta 管辖的店铺/品牌：{shop_str}。"
    try:
        reply = llm_client.chat(
            [{"role": "system", "content": AGENT_PERSONA + "\n\n" + ctx},
             {"role": "user", "content": text or "你好"}],
            max_tokens=400, temperature=0.4)
        reply = (reply or "").strip()
        if reply:
            return reply
    except llm_client.LLMUnavailable:
        pass
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------- 补货拉取（赛狐）

def _managed_brand_names(db, op):
    """运营管辖品牌名（小写），用于匹配采购计划的店铺名 / SKU 前缀。"""
    names = []
    if op and op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
        for bid in ids:
            b = db.get(Brand, bid)
            if b and b.name:
                names.append(b.name.strip().lower())
    return names


def build_restock(db, op, statuses=("待采购", "待审核")):
    """从赛狐拉补货（采购计划），过滤到运营管辖店铺 + 指定状态 → 精简 plans。

    匹配品牌：采购计划 item 的 shop_name 或 sku 前缀含管辖品牌名（serenorch/huhole…）。
    管理员（无 scope）→ 不过滤全看；非管理员且无 scope → 看不到（保守）。
    """
    from ..services import purchase_plan_service as pp
    names = _managed_brand_names(db, op)
    is_admin = bool(op and op.is_admin)
    try:
        res = pp.list_plans(db, page=1, page_size=100)
    except Exception:
        return []
    out = []
    for g in res.get("plans", []):
        if g.get("status_label") not in statuses:
            continue
        items = g.get("items") or []
        shops = [(it.get("shop_name") or "").lower() for it in items]
        skus = [str(it.get("sku") or it.get("msku") or "") for it in items]
        if is_admin:
            pass
        elif not names:
            continue
        elif not (any(nm in s for nm in names for s in shops)
                  or any(sk.lower().startswith(nm) for nm in names for sk in skus)):
            continue
        matched_brand = next((nm for nm in names
                              if any(nm in s for s in shops)
                              or any(sk.lower().startswith(nm) for sk in skus)), "")
        sku_items = [{
            "sku": it.get("sku") or it.get("msku") or "",
            "name": it.get("commodity_name") or "",
            "qty": it.get("plan_num") or 0,
        } for it in items]
        out.append({
            "plan_group_no": g.get("plan_group_no"),
            "status_label": g.get("status_label"),
            "shop_name": next((s for s in (it.get("shop_name") for it in items) if s), ""),
            "brand": matched_brand,
            "item_count": g.get("item_count"),
            "total_qty": g.get("plan_total_num"),
            "items": sku_items,
        })
    return out


def build_progress(db, op):
    """运营管辖品牌的批次进度（名称/店铺/状态）。管理员看全部。"""
    ids = []
    if op and op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
    q = db.query(Batch)
    if not (op and op.is_admin):
        if not ids:
            return []
        q = q.filter(Batch.brand_id.in_(ids))
    out = []
    for b in q.order_by(Batch.id.desc()).limit(20).all():
        brand = db.get(Brand, b.brand_id) if b.brand_id else None
        out.append({"name": b.name, "brand": brand.name if brand else "",
                    "status": b.status, "batch_id": b.id})
    return out


# ---------------------------------------------------------------- 消息入口

def handle_message(db, *, chat_id, open_id=None, user_id=None, text=""):
    """运营发来文本消息 → 先认人，再按意图：明确任务给结构化卡片，其余文本对话。

    返回 {reply|card, sent, intent, operator}。
    """
    op = intake.identify_operator(db, open_id=open_id, user_id=user_id)
    if op is None:
        msg = ("你好，我是亚马逊发货管家。但我还没找到你的运营档案——"
               "请管理员把你的飞书账号和管辖店铺登记后，再来找我。")
        return {"reply": msg, "sent": _safe_send_text(chat_id, msg), "operator": None}

    sess = intake.ensure_session(db, op, chat_id, open_id or "")
    intent = parse_intent(text)
    sess.last_intent = intent["intent"]
    db.commit()

    # 看本周补货 / 待采购 → 从赛狐拉补货计划（按管辖店铺过滤）→ 补货卡片
    if intent["intent"] == "restock":
        plans = build_restock(db, op)
        card = cards.restock_card(op.name or "运营", plans)
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "operator": op.id, "plans": len(plans), "intent": "restock"}

    # 明确"看建仓待办 / 建仓就绪体检"诉求 → 建仓待办卡片
    if intent["intent"] == "intake":
        data = intake.build_intake(db, op, chat_id, open_id or "")
        card = cards.todo_card(op.name or "运营", data["todos"])
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "operator": op.id, "todos": data["todos"], "intent": "intake"}

    # 其余（寒暄 / 帮助 / 泛问 / 未识别）→ 结构化欢迎卡片：我是谁 + 你管哪些店 + 能帮你做什么 + 怎么开始
    card = cards.welcome_card(op.name or "运营", _managed_shops(db, op))
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "operator": op.id, "intent": intent["intent"]}


# ---------------------------------------------------------------- 按钮动作入口

def handle_action(db, *, chat_id, open_id, action, value):
    """卡片按钮回调。value 是按钮 value dict（含 action + 参数）。

    返回 {card, sent, action}。每个动作产出一张结果/下一步卡片。
    """
    value = value or {}
    batch_id = value.get("batch_id")
    inquiry_id = value.get("inquiry_id")
    quote_id = value.get("quote_id")

    if action == "restock":
        return _act_restock(db, chat_id, open_id)
    if action == "view_progress":
        return _act_progress(db, chat_id, open_id)
    if action == "confirm_purchase":
        return _act_confirm_purchase(db, chat_id, value)
    if action == "fill_data":
        return _act_fill_data(db, chat_id, batch_id)
    if action == "start_inquiry":
        return _act_start_inquiry(db, chat_id, batch_id)
    if action == "send_inquiry":
        return _act_send_inquiry(db, chat_id, inquiry_id)
    if action == "cancel_inquiry":
        return _act_cancel_inquiry(db, chat_id, inquiry_id)
    if action == "view_comparison":
        return _act_comparison(db, chat_id, inquiry_id)
    if action == "choose_forwarder":
        return _act_choose(db, chat_id, inquiry_id, quote_id)

    card = cards.text_card("未知操作", f"无法识别的动作：{action}", template="red")
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": action}


def _batch_name(db, batch_id):
    b = db.get(Batch, batch_id) if batch_id else None
    return (b.name if b and b.name else (f"批次#{batch_id}" if batch_id else "?"))


def _act_restock(db, chat_id, open_id):
    """欢迎卡片【查看本周补货】按钮 → 认人 → 拉赛狐补货 → 补货卡片。"""
    op = intake.identify_operator(db, open_id=open_id)
    if op is None:
        card = cards.text_card("未登记", "未找到你的运营档案。", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "restock"}
    plans = build_restock(db, op)
    card = cards.restock_card(op.name or "运营", plans)
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "restock", "plans": len(plans)}


def _act_progress(db, chat_id, open_id):
    """欢迎卡片【查看进度】按钮 → 认人 → 管辖批次进度 → 进度卡片。"""
    op = intake.identify_operator(db, open_id=open_id)
    if op is None:
        card = cards.text_card("未登记", "未找到你的运营档案。", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "view_progress"}
    items = build_progress(db, op)
    card = cards.progress_card(op.name or "运营", items)
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "view_progress", "items": len(items)}


def _act_confirm_purchase(db, chat_id, value):
    """补货卡片【确认采购】→ 先显示下一步说明（真实写采购单待接入，关键动作不自动执行）。"""
    pgn = (value or {}).get("plan_group_no") or "?"
    body = (
        f"**采购计划**：{pgn}\n\n"
        "**下一步会做**：走现有采购确认流程 —— 在系统/赛狐对该计划确认采购、生成采购单（工厂下单）。\n"
        "到货后回来发 **「建仓」**，把这批做成亚马逊入库计划、拿分仓目的仓，再进 **询价**。\n\n"
        "⚠️ 「确认采购」会向赛狐写采购单，属关键动作 —— 真实执行待接入后由你在此一键确认。"
    )
    card = cards.text_card("下一步 · 确认采购", body, template="orange")
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}


def _act_fill_data(db, chat_id, batch_id):
    from ..services import batch_prep_service as prep
    b = db.get(Batch, batch_id)
    if b is None:
        card = cards.text_card("补数据", "批次不存在", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "fill_data"}
    try:
        res = prep.fill_missing_products(db, b)
        agg = prep.aggregate(db, b)
        body = (f"已尝试回填 **{_batch_name(db, batch_id)}**：新建 {len(res['created'])} 个、"
                f"刷新 {len(res['refreshed'])} 个产品。\n"
                + ("✅ 现在数据就绪，可发起询价。" if agg["ready"]
                   else "仍有待补项：\n" + "\n".join("· " + s for s in agg["issues"][:6])))
        tmpl = "green" if agg["ready"] else "orange"
        card = cards.text_card("补数据结果", body, template=tmpl)
    except Exception as e:
        card = cards.text_card("补数据失败", f"回填出错：{str(e)[:160]}", template="red")
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "fill_data"}


def _act_start_inquiry(db, chat_id, batch_id):
    """起草询价（调询价线接缝），回"待确认"卡片——不自动群发（红线）。"""
    if batch_id is None:
        card = cards.text_card("发起询价", "缺批次参数", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "start_inquiry"}
    try:
        inq = port.start_inquiry(db, batch_id)
    except Exception as e:
        card = cards.text_card("发起询价失败", f"询价线返回错误：{str(e)[:160]}", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "start_inquiry"}
    inquiry_id = getattr(inq, "id", None) or (inq.get("id") if isinstance(inq, dict) else None)
    content = getattr(inq, "content", None) or (inq.get("content") if isinstance(inq, dict) else "")
    fwd_names = _target_forwarder_names(db, inq)
    note = "（询价线未接入，下方为占位草稿）\n\n" if port.using_stub() else ""
    card = cards.inquiry_drafted_card(_batch_name(db, batch_id), inquiry_id,
                                      note + (content or ""), fwd_names)
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "start_inquiry", "inquiry_id": inquiry_id}


def _target_forwarder_names(db, inq):
    from ..models import Forwarder
    ids = getattr(inq, "target_forwarder_ids", None)
    if isinstance(inq, dict):
        ids = inq.get("target_forwarder_ids")
    if not ids:
        return []
    try:
        ids = json.loads(ids) if isinstance(ids, str) else list(ids)
    except (ValueError, TypeError):
        return []
    names = []
    for fid in ids:
        f = db.get(Forwarder, fid)
        if f:
            names.append(f.name)
    return names


def _act_send_inquiry(db, chat_id, inquiry_id):
    if inquiry_id is None:
        card = cards.text_card("群发", "缺询价参数", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "send_inquiry"}
    try:
        res = port.send_inquiry(db, inquiry_id)
    except Exception as e:
        card = cards.text_card("群发失败", f"询价线返回错误：{str(e)[:160]}", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "send_inquiry"}
    body = "✅ 已群发询价，等货代回复。回复到齐后可点下方查看比价。"
    if isinstance(res, dict) and res.get("stub"):
        body = "⚠️ 询价线未接入：已占位标记为已发送（未真正群发）。"
    card = cards.text_card("询价已发出", body, template="green")
    card["elements"].append({"tag": "action", "actions": [{
        "tag": "button", "text": {"tag": "plain_text", "content": "查看比价"},
        "type": "default", "value": {"action": "view_comparison", "inquiry_id": inquiry_id},
    }]})
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "send_inquiry", "result": res}


def _act_cancel_inquiry(db, chat_id, inquiry_id):
    inq = db.get(Inquiry, inquiry_id) if inquiry_id else None
    if inq is not None:
        inq.status = "已取消"
        db.commit()
    card = cards.text_card("已取消", "本次询价已取消，未发出。", template="grey")
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "cancel_inquiry"}


def _act_comparison(db, chat_id, inquiry_id):
    if inquiry_id is None:
        card = cards.text_card("比价", "缺询价参数", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "view_comparison"}
    try:
        comp = port.get_comparison(db, inquiry_id)
    except Exception as e:
        card = cards.text_card("比价失败", f"询价线返回错误：{str(e)[:160]}", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "view_comparison"}
    inq = db.get(Inquiry, inquiry_id)
    bname = _batch_name(db, inq.batch_id if inq else None)
    card = cards.comparison_card(bname, inquiry_id, comp)
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "view_comparison", "comparison": comp}


def _act_choose(db, chat_id, inquiry_id, quote_id):
    """人工选定货代——这是红线里"选定"动作，必须由按钮触发（此处即是）。"""
    if inquiry_id is None or quote_id is None:
        card = cards.text_card("选定", "缺参数", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "choose_forwarder"}
    try:
        port.choose_forwarder(db, inquiry_id, quote_id)
    except Exception as e:
        card = cards.text_card("选定失败", f"询价线返回错误：{str(e)[:160]}", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "choose_forwarder"}
    card = cards.text_card("已选定货代",
                           f"✅ 已记录你选定的货代（报价 #{quote_id}）。"
                           "后续派单/发文件仍需在系统内人工确认。", template="green")
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "choose_forwarder"}
