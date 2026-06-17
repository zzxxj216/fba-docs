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
from ..models import Batch, Inquiry

# 意图集合：intake=建仓就绪体检/看待办；inquiry=发起询价；compare=看比价；help/unknown
INTENTS = ("intake", "inquiry", "compare", "fill_data", "help", "unknown")

_KEYWORDS = {
    "intake": ["待办", "体检", "建仓", "就绪", "开始", "我管", "待采购", "今天", "拉一下", "看看"],
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
                   "intake(看待办/建仓就绪体检)、inquiry(发起询价)、compare(看货代比价)、"
                   "fill_data(补全数据)、help(帮助)、unknown。"
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
    return "intake"            # 默认当作"看待办"，运营唤醒即给体检


def _safe_send_card(chat_id, card):
    """发卡片；飞书未接通时不崩（返回 None），便于离线/测试。"""
    if not fc.configured():
        return None
    try:
        return fc.send_card(chat_id, card)
    except RuntimeError:
        return None


# ---------------------------------------------------------------- 消息入口

def handle_message(db, *, chat_id, open_id=None, user_id=None, text=""):
    """运营发来文本消息。返回 {card, sent, ...}（card 也已尝试发出）。"""
    op = intake.identify_operator(db, open_id=open_id, user_id=user_id)
    if op is None:
        card = cards.text_card(
            "未登记", "未找到你的运营档案。请管理员在 feishu_operators 登记你的"
            "飞书账号与管辖店铺后再试。", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "operator": None}

    intent = parse_intent(text)
    sess = intake.ensure_session(db, op, chat_id, open_id or "")
    sess.last_intent = intent["intent"]
    db.commit()

    if intent["intent"] == "help":
        card = cards.text_card(
            "我能帮你", "- 发「待办」或「开始建仓」→ 给你管辖店铺的建仓就绪体检\n"
            "- 体检卡片里点【开始建仓询价】→ 起草询价（人工确认后群发）\n"
            "- 点【帮我补数据】→ 自动回填产品资料\n"
            "- 询价回来后给你整包比价，**由你点选定**")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "operator": op.id}

    # intake / inquiry / compare / fill_data / unknown 都先给待办体检卡片
    data = intake.build_intake(db, op, chat_id, open_id or "")
    card = cards.todo_card(op.name or "运营", data["todos"])
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "operator": op.id, "todos": data["todos"], "intent": intent["intent"]}


# ---------------------------------------------------------------- 按钮动作入口

def handle_action(db, *, chat_id, open_id, action, value):
    """卡片按钮回调。value 是按钮 value dict（含 action + 参数）。

    返回 {card, sent, action}。每个动作产出一张结果/下一步卡片。
    """
    value = value or {}
    batch_id = value.get("batch_id")
    inquiry_id = value.get("inquiry_id")
    quote_id = value.get("quote_id")

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
