"""飞书线编排 —— 把"消息/按钮"翻译成动作，调 intake / 询价线接缝，产出回复卡片。

- parse_intent(text)        用 llm_client 理解运营自然语言意图；无 LLM 时关键词兜底。
- handle_message(...)       运营发消息 → 认人 → intake → 待办卡片（或提示）。
- handle_action(...)        卡片按钮回调 → 分发到 intake/询价线 → 结果卡片。

安全红线（CONTRACT §4）：一切"群发/选定/发文件"只在按钮动作里触发，且对应飞书 UI
本就是"人工点击"——本模块不自动替用户点。
"""

import json
import os

from .. import feishu_client as fc
from .. import llm_client
from . import cards, intake, workspace
from . import inquiry_port as port
from ..models import Batch, Brand, Inquiry

# 意图集合：restock=看本周补货/待采购（赛狐）；intake=建仓就绪体检/看待办；
# inquiry=发起询价；compare=看比价；help/unknown
INTENTS = ("restock", "intake", "summary", "inquiry", "compare", "labels",
           "fill_data", "help", "unknown")

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
    "summary": ["汇总", "本周汇总", "整包", "分仓汇总", "汇总询价"],
    "inquiry": ["询价", "报价", "发货代", "找货代"],
    "compare": ["比价", "对比", "哪家", "选货代", "报价对比"],
    "labels": ["箱唛", "标签", "打标", "fnsku", "FNSKU", "贴标", "条码", "下载标签"],
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
                   "summary(看本周整包询价汇总——所有已建仓批次的分仓FC汇总到一起)、"
                   "labels(下载箱唛/FNSKU标签、打标贴标)、"
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


def build_weekly_summary(db, op):
    """汇总运营本周所有已建仓批次的分仓信息（方案B：每批列出所有方案涉及的全部 FC）。

    每个 FC 的箱数/重量取它在各方案里出现过的最大值做代表（够货代报头程参考）。
    返回 {batch_count, fc_count, batches:[{batch_id,name,store,option_count,fcs:[...]}]}。
    """
    ids = []
    if op and op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
    q = db.query(Batch).filter(Batch.placement_options.isnot(None))
    if not (op and op.is_admin):
        if not ids:
            return {"batch_count": 0, "fc_count": 0, "batches": []}
        q = q.filter(Batch.brand_id.in_(ids))
    batches, all_fcs = [], set()
    for b in q.order_by(Batch.id.desc()).all():
        try:
            opts = json.loads(b.placement_options or "[]")
        except (ValueError, TypeError):
            opts = []
        if not opts:
            continue
        fc_map = {}
        for o in opts:
            for s in (o.get("shipments") or []):
                fc = s.get("fc")
                if not fc:
                    continue
                cur = fc_map.get(fc) or {"fc": fc, "boxes": 0, "weight_kg": 0.0}
                cur["boxes"] = max(cur["boxes"], s.get("boxes") or 0)
                cur["weight_kg"] = max(cur["weight_kg"], s.get("weight_kg") or 0)
                fc_map[fc] = cur
        if not fc_map:
            continue
        brand = db.get(Brand, b.brand_id) if b.brand_id else None
        all_fcs.update(fc_map)
        batches.append({"batch_id": b.id, "name": b.name,
                        "store": b.shop_name or (brand.name if brand else ""),
                        "option_count": len(opts),
                        "fcs": sorted(fc_map.values(), key=lambda x: x["fc"])})
    return {"batch_count": len(batches), "fc_count": len(all_fcs), "batches": batches}


def _brand_forwarders(db, brand_id):
    """品牌绑定的货代（委托 inquiry_service，避免重复绑定逻辑）。"""
    from ..services import inquiry_service as s
    return s._brand_forwarders(db, brand_id)


def _weekly_built_batches(db, op):
    """运营本周所有已建仓批次 [(batch, placement_options)]。"""
    ids = []
    if op and op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
    q = db.query(Batch).filter(Batch.placement_options.isnot(None))
    if not (op and op.is_admin):
        if not ids:
            return []
        q = q.filter(Batch.brand_id.in_(ids))
    out = []
    for b in q.order_by(Batch.id.desc()).all():
        try:
            opts = json.loads(b.placement_options or "[]")
        except (ValueError, TypeError):
            opts = []
        if opts:
            out.append((b, opts))
    return out


def weekly_comparison(db, op, quotes):
    """整包比价（方案B）：给各货代逐 FC 头程报价，算每家承接整包的总价。

    quotes: {货代名: {FC: {"rate": float, "unit": "/kg"|"/box", "currency": "USD"}}}
    每批挑成本最低且全覆盖的分仓方案(placement费 + 该方案各FC头程)；
    货代整包总价 = Σ各批最优方案。返回按总价排序的列表（缺仓不全覆盖的排后）。
    """
    batches = _weekly_built_batches(db, op)
    result = []
    for fname, frates in (quotes or {}).items():
        currency = next((v.get("currency", "USD") for v in frates.values()), "USD")
        per_batch, grand_total, full = [], 0.0, True
        for b, opts in batches:
            best = None
            for o in opts:
                head, miss = 0.0, []
                for s in (o.get("shipments") or []):
                    fc = s.get("fc")
                    r = frates.get(fc)
                    if not r:
                        miss.append(fc)
                        continue
                    qty = (s.get("weight_kg") or 0) if "kg" in r.get("unit", "") else (s.get("boxes") or 0)
                    head += qty * (r.get("rate") or 0)
                total = (o.get("fee_usd") or 0) + head
                if not miss and (best is None or total < best["total"]):
                    best = {"option": o.get("label"), "option_id": o.get("placement_option_id"),
                            "fee": round(o.get("fee_usd") or 0, 1),
                            "head_haul": round(head, 1), "total": round(total, 1)}
            if best is None:
                full = False
                per_batch.append({"batch": b.name, "batch_id": b.id,
                                  "error": "所有方案都有缺仓未报价"})
            else:
                grand_total += best["total"]
                per_batch.append({"batch": b.name, "batch_id": b.id, **best})
        result.append({"forwarder": fname, "currency": currency,
                       "total": round(grand_total, 1), "coverage_ok": full,
                       "per_batch": per_batch})
    result.sort(key=lambda x: (not x["coverage_ok"], x["total"]))
    for i, r in enumerate(result):
        r["recommended"] = (i == 0 and r["coverage_ok"])
    return result


def weekly_comparison_from_db(db, op):
    """读最近一条整包询价(weekly)的真实货代报价(QuoteLine) → 整包比价。

    返回 {inquiry, ref_code, comparison, forwarders}。无整包询价/无报价时 comparison=[]。
    """
    from ..models import Inquiry, InquiryQuote
    ids = []
    if op and op.scope_brand_ids:
        try:
            ids = json.loads(op.scope_brand_ids) or []
        except (ValueError, TypeError):
            ids = []
    inq = None
    for cand in db.query(Inquiry).order_by(Inquiry.id.desc()).limit(60).all():
        try:
            st = json.loads(cand.structured or "{}")
        except (ValueError, TypeError):
            st = {}
        if not st.get("weekly"):
            continue
        if op and not op.is_admin:
            b = db.get(Batch, cand.batch_id)
            if not ids or (b and b.brand_id not in ids):
                continue
        inq = cand
        break
    if inq is None:
        return {"inquiry": None, "ref_code": "", "comparison": [], "forwarders": 0}
    quotes = {}
    for qt in db.query(InquiryQuote).filter(InquiryQuote.inquiry_id == inq.id).all():
        frates = {}
        for ln in qt.lines:
            fc = (ln.fc or "").upper()
            if fc in ("", "ALL") or ln.price is None:
                continue
            frates[fc] = {"rate": ln.price, "unit": ln.unit or "/kg",
                          "currency": ln.currency or "USD"}
        if frates:
            name = qt.forwarder.name if qt.forwarder else f"货代#{qt.forwarder_id}"
            quotes[name] = frates
    return {"inquiry": inq.id, "ref_code": inq.ref_code,
            "comparison": weekly_comparison(db, op, quotes), "forwarders": len(quotes)}


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

    # 本周整包询价汇总 → 汇总所有已建仓批次的全部 FC + 写进主文件 → 汇总卡片
    if intent["intent"] == "summary":
        summary = build_weekly_summary(db, op)
        try:
            workspace.write_weekly_summary(op.name or "运营", summary)
        except Exception:
            pass
        card = cards.summary_card(op.name or "运营", summary)
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "operator": op.id, "intent": "summary",
                "batch_count": summary.get("batch_count")}

    # 整包比价：读最近整包询价的真实货代报价(QuoteLine) → 比价卡片
    if intent["intent"] == "compare":
        data = weekly_comparison_from_db(db, op)
        if not data.get("comparison"):
            card = cards.text_card("整包比价", "还没有货代报价入库。先「整包询价」群发，"
                                   "货代回价会自动提取；之后再发「比价」。", template="orange")
        else:
            card = cards.weekly_comparison_card(data["comparison"])
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "operator": op.id, "intent": "compare", "forwarders": data.get("forwarders")}

    # 标签下载：列已建仓批次 → 箱唛/FNSKU 下载剪切
    if intent["intent"] == "labels":
        built = [{"batch_id": bt.id, "name": bt.name} for bt, _ in _weekly_built_batches(db, op)]
        card = cards.labels_card(op.name or "运营", built)
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "operator": op.id, "intent": "labels", "batches": len(built)}

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
    if action == "view_placement":
        return _act_view_placement(db, chat_id, batch_id)
    if action == "weekly_inquiry":
        return _act_weekly_inquiry(db, chat_id, open_id)
    if action == "weekly_inquiry_send":
        return _act_weekly_inquiry_send(db, chat_id, open_id)
    if action == "choose_weekly":
        return _act_choose_weekly(db, chat_id, open_id, value)
    if action == "commit_placement":
        return _act_commit_placement(db, chat_id, value)
    if action == "fetch_labels":
        return _act_fetch_labels(db, chat_id, value)
    if action == "confirm_purchase":
        return _act_confirm_purchase(db, chat_id, open_id, value)
    if action == "build":
        return _act_build(db, chat_id, open_id, value)
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


def _btn_dict(text, action, value_extra=None, btn_type="default"):
    """内联按钮 dict（service 里手动拼卡片用）。"""
    v = {"action": action}
    if value_extra:
        v.update(value_extra)
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": btn_type, "value": v}


def _act_view_placement(db, chat_id, batch_id):
    """进度卡片【查看分仓方案】→ 读批次已存的 placement_options → 分仓方案卡片（只读）。"""
    b = db.get(Batch, batch_id) if batch_id else None
    if b is None:
        card = cards.text_card("分仓方案", "批次不存在", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "view_placement"}
    try:
        opts = json.loads(b.placement_options or "[]")
    except (ValueError, TypeError):
        opts = []
    card = cards.placement_card(b.name or f"批次#{batch_id}", opts, batch_id)
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "view_placement", "options": len(opts)}


def _weekly_draft_text(summary):
    """整包询价正文（汇总各店铺全部 FC）。"""
    lines = ["老师好，本周一批 FBA 头程货需要整体询价，目的仓及货量如下："]
    for b in summary.get("batches") or []:
        fcs = "、".join(f"{f['fc']}({f.get('boxes', 0)}箱/{f.get('weight_kg', 0)}kg)"
                       for f in (b.get("fcs") or []))
        lines.append(f"【{b.get('store') or b.get('name')}】{fcs}")
    lines.append("麻烦按每个目的仓分别报：运费单价(币种/单位)、渠道、时效、截关、起送费；"
                 "另外贵司月度报关费怎么收?多谢～")
    return "\n".join(lines)


def _act_weekly_inquiry(db, chat_id, open_id):
    """汇总卡片【整包询价】→ 起草整包询价正文 → 待确认卡片（不自动群发，红线）。"""
    op = intake.identify_operator(db, open_id=open_id)
    summary = build_weekly_summary(db, op)
    if not summary.get("batches"):
        card = cards.text_card("整包询价", "本周还没有已建仓批次,先建仓再汇总询价。", template="orange")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "weekly_inquiry"}
    # 预览将发给哪些货代（本周各批次品牌绑定的货代，去重）
    fwd_names = []
    seen = set()
    for b in summary["batches"]:
        bt = db.get(Batch, b["batch_id"])
        for f in (_brand_forwarders(db, bt.brand_id) if bt else []):
            if f.id not in seen:
                seen.add(f.id)
                fwd_names.append(f.name)
    draft = _weekly_draft_text(summary)
    body = (f"**整包询价草稿**（{summary['batch_count']} 批 · {summary['fc_count']} 个目的仓）\n"
            f"**将发给**：{('、'.join(fwd_names)) or '（无绑定货代）'}\n\n{draft}")
    card = cards.text_card("整包询价 · 待确认", body, template="orange")
    card["elements"].append({"tag": "action", "actions": [
        _btn_dict("✅ 确认群发货代", "weekly_inquiry_send", {}, "primary"),
    ]})
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "weekly_inquiry"}


def _act_weekly_inquiry_send(db, chat_id, open_id):
    """【确认群发货代】→ 把整包询价正文真发给本周各批次绑定的货代（企微）。"""
    op = intake.identify_operator(db, open_id=open_id)
    summary = build_weekly_summary(db, op)
    if not summary.get("batches"):
        card = cards.text_card("整包询价", "本周无已建仓批次。", template="orange")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "weekly_inquiry_send"}
    # 合并目的仓 lanes（去重，箱数/重量取最大）+ 去重货代 + 代表批次集
    batches, lanes_map, fwds = [], {}, {}
    for b in summary["batches"]:
        bt = db.get(Batch, b["batch_id"])
        if bt is None:
            continue
        batches.append(bt)
        for f in (b.get("fcs") or []):
            fc = f.get("fc")
            cur = lanes_map.get(fc) or {"fc": fc, "boxes": 0, "weight_kg": 0.0}
            cur["boxes"] = max(cur["boxes"], f.get("boxes") or 0)
            cur["weight_kg"] = max(cur["weight_kg"], f.get("weight_kg") or 0)
            lanes_map[fc] = cur
        for fwd in _brand_forwarders(db, bt.brand_id):
            fwds[fwd.id] = fwd
    if not batches:
        card = cards.text_card("整包询价", "没有可询价批次。", template="orange")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "weekly_inquiry_send"}
    from datetime import date
    from ..services import inquiry_service as inq_svc
    ref = f"INQ-周{date.today().strftime('%m%d')}-{batches[0].id}"
    draft = _weekly_draft_text(summary) + f"\n（报价请带上暗号 {ref}，方便归属）"
    inq = inq_svc.start_weekly_inquiry(db, batches, list(lanes_map.values()), list(fwds), draft, ref)
    res = inq_svc.send_inquiry(db, inq.id)
    sent = [r["name"] for r in res["results"] if r.get("sent")]
    failed = [f"{r['name']}({str(r.get('error', ''))[:30]})" for r in res["results"] if not r.get("sent")]
    body = (f"✅ 整包询价已群发 **{len(sent)}** 家货代：{('、'.join(sent)) or '（无绑定货代）'}\n"
            f"暗号 `{ref}`\n"
            + (f"⚠️ 失败：{('；'.join(failed))}\n" if failed else "")
            + "货代回价会**自动提取入库**；发「比价」看整包比价。")
    card = cards.text_card("整包询价已发出", body, template="green" if sent else "red")
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "weekly_inquiry_send", "inquiry_id": inq.id, "sent_count": len(sent)}


def _act_commit_placement(db, chat_id, value):
    """分仓方案卡片【提交此方案+配自送】→ confirm_placement_for_batch。

    **默认演练(dry-run)，不碰亚马逊**；仅当环境变量 INBOUND_LIVE_SUBMIT=1 才真实提交。
    """
    batch_id = (value or {}).get("batch_id")
    poid = (value or {}).get("placement_option_id")
    b = db.get(Batch, batch_id) if batch_id else None
    if b is None:
        card = cards.text_card("提交分仓", "批次不存在", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "commit_placement"}
    live = os.getenv("INBOUND_LIVE_SUBMIT") == "1"
    try:
        from ..services import inbound_service as ib
        plan = ib.confirm_placement_for_batch(db, b, poid, live=live)
    except Exception as e:
        card = cards.text_card("提交分仓失败", str(e)[:300], template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "commit_placement"}
    if plan.get("dry_run"):
        body = (f"🧪 **演练（未真实提交亚马逊）**\n"
                f"方案 `{poid}`　仓：{('、'.join(plan.get('fcs') or [])) or '—'}\n"
                f"将执行：{' → '.join(plan.get('steps') or [])}\n\n"
                "真实提交需运维开启 `INBOUND_LIVE_SUBMIT=1`（确认无误再开）。")
        card = cards.text_card("提交分仓 · 演练", body, template="blue")
    else:
        ships = plan.get("shipments") or []
        body = (f"✅ 已向亚马逊提交分仓 + 配自送：**{len(ships)}** 个货件\n"
                + "\n".join(f"　🏬 {s.get('fc')}　{s.get('confirmationId') or ''}　{s.get('status') or ''}"
                            for s in ships[:15]))
        card = cards.text_card("分仓已提交 + 自送已配", body, template="green")
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "commit_placement", "dry_run": plan.get("dry_run", True)}


def _act_fetch_labels(db, chat_id, value):
    """标签卡片【箱唛】/【FNSKU】→ inbound_service.fetch_labels（默认演练，不碰亚马逊）。"""
    batch_id = (value or {}).get("batch_id")
    kind = (value or {}).get("kind") or "box"
    b = db.get(Batch, batch_id) if batch_id else None
    if b is None:
        card = cards.text_card("标签下载", "批次不存在", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "fetch_labels"}
    live = os.getenv("INBOUND_LIVE_SUBMIT") == "1"
    kname = "箱唛" if kind == "box" else "FNSKU 商品标签"
    try:
        from ..services import inbound_service as ib
        plan = ib.fetch_labels(db, b, kind=kind, live=live)
    except Exception as e:
        card = cards.text_card("标签下载失败", str(e)[:300], template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "fetch_labels"}
    if plan.get("dry_run"):
        body = (f"🧪 **演练（未真实下载）**　{kname}\n页型 `{plan.get('page_type')}`\n"
                f"将执行：{' → '.join(plan.get('steps') or [])}\n\n"
                "真实下载需运维开 `INBOUND_LIVE_SUBMIT=1`；下载后自动剪切成单张 PDF。")
        card = cards.text_card("标签下载 · 演练", body, template="blue")
    else:
        saved = plan.get("saved") or []
        total = plan.get("total_labels") or sum(s.get("labels") or 0 for s in saved)
        body = (f"✅ {kname} 已下载并剪切：**{total}** 张单标签（{len(saved)} 个货件/份）\n"
                + (f"📦 打包：`{os.path.basename(plan.get('zip'))}`（发货代一个文件就行）\n" if plan.get("zip") else "")
                + (f"🖨️ 合并一次打印：`{os.path.basename(plan.get('merged_pdf'))}`\n" if plan.get("merged_pdf") else "")
                + f"归档目录：`{plan.get('out_dir')}`\n"
                + "\n".join(f"　{s.get('shipment', 'FNSKU')}：{s.get('labels')} 张"
                            for s in saved[:15]))
        card = cards.text_card("标签已下载（已剪切单张）", body, template="green")
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "fetch_labels", "dry_run": plan.get("dry_run", True)}


def _act_choose_weekly(db, chat_id, open_id, value):
    """整包比价【选定这家】→ 记录选定货代（本地留痕）。

    真实"向亚马逊提交各批最优分仓方案 + 配置自送运输"属关键写操作，待接入后逐批人工确认，
    此处先不自动提交（红线 + 上次误操作教训）。
    """
    fname = (value or {}).get("forwarder") or "?"
    op = intake.identify_operator(db, open_id=open_id)
    data = weekly_comparison_from_db(db, op)
    chosen = next((r for r in (data.get("comparison") or []) if r.get("forwarder") == fname), None)
    if chosen is None:
        card = cards.text_card("选定货代", f"没找到 {fname} 的比价结果，先发「比价」再选。", template="orange")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "choose_weekly"}
    live = os.getenv("INBOUND_LIVE_SUBMIT") == "1"
    from ..services import inbound_service as ib
    lines = [f"✅ 选定货代 **{fname}**（整包 {chosen.get('total')} {chosen.get('currency', 'USD')}）",
             "逐批提交最优分仓方案 + 配自送：" + ("**真实提交亚马逊**" if live else "**演练（不碰亚马逊）**")]
    for pb in chosen.get("per_batch") or []:
        bid, oid = pb.get("batch_id"), pb.get("option_id")
        if not bid or not oid:
            lines.append(f"　⚠️ {pb.get('batch')}：{pb.get('error') or '缺方案ID，跳过'}")
            continue
        b = db.get(Batch, bid)
        try:
            plan = ib.confirm_placement_for_batch(db, b, oid, live=live)
            if plan.get("dry_run"):
                lines.append(f"　🧪 {pb.get('batch')}：方案[{pb.get('option')}] 演练OK（{len(plan.get('fcs') or [])} 仓）")
            else:
                lines.append(f"　✅ {pb.get('batch')}：已提交+配自送（{len(plan.get('shipments') or [])} 货件）")
        except Exception as e:
            lines.append(f"　❌ {pb.get('batch')}：{str(e)[:60]}")
    if not live:
        lines.append("\n真实提交需运维开 `INBOUND_LIVE_SUBMIT=1`（确认无误再开）。")
    card = cards.text_card("选定货代 + 逐批提交分仓", "\n".join(lines), template="green")
    return {"card": card, "sent": _safe_send_card(chat_id, card),
            "action": "choose_weekly", "forwarder": fname, "live": live}


def _act_confirm_purchase(db, chat_id, open_id, value):
    """补货卡片【确认采购】→ 一键：工厂确认 → 生成采购单(赛狐到「待到货」) → 直接建仓 → 分仓方案。

    每步写工作区(总览/概况/日志)；某步失败记错误、已成的不回滚，卡片如实显示进度。
    """
    pgn = (value or {}).get("plan_group_no")
    if not pgn:
        card = cards.text_card("确认采购", "缺计划号", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}
    op = intake.identify_operator(db, open_id=open_id)
    opname = (op.name if op else None) or "运营"
    from ..services import purchase_order_service as pos
    from ..services import purchase_plan_service as pps
    from ..services import inbound_service

    grp = pps._find_group(db, pgn)
    if grp is None:
        card = cards.text_card("确认采购", f"赛狐未找到采购计划 {pgn}（近 60 天）", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}
    pitems = grp.get("purchasePlanItemVoList") or []
    shop = next((p.get("shopName") for p in pitems if p.get("shopName")), "") or ""
    sku_count = len(pitems)
    qty = sum((p.get("planNum") or 0) for p in pitems)
    workspace.record_plan(opname, pgn, shop=shop, sku_count=sku_count, qty=qty, stage="待采购")
    steps = []

    # 1) 工厂确认（本地）
    try:
        items = [{"sku": p.get("sku"), "commodity_name": p.get("commodityName"),
                  "num": p.get("planNum"), "box_count": p.get("cartonNum")} for p in pitems]
        pos.confirm_plan(db, pgn, items, confirmed_by=opname)
        workspace.record_plan(opname, pgn, stage="已确认采购")
        steps.append("✅ 工厂确认")
    except Exception as e:
        db.rollback()
        workspace.log(opname, pgn, f"工厂确认失败：{e}", level="error", shop=shop)
        card = cards.text_card("确认采购失败", f"工厂确认出错：{str(e)[:160]}", template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}

    # 2) 生成采购单 + 下单到「待到货」（赛狐写，action=2）
    try:
        pos.create_purchase_order(db, pgn, action="2", dry_run=False)
        info = pos.get_confirm(db, pgn)
        workspace.record_plan(opname, pgn, stage="采购单已生成",
                              note=f"采购单 {info.get('purchase_no', '')} · {info.get('status', '')}")
        steps.append(f"✅ 采购单已生成 → {info.get('status', '待到货')}（{info.get('purchase_no', '')}）")
    except RuntimeError as e:
        db.rollback()
        workspace.log(opname, pgn, f"生成采购单失败：{e}", level="error", shop=shop)
        steps.append(f"❌ 生成采购单失败：{str(e)[:140]}")
        card = cards.text_card("确认采购 · 部分完成", "\n".join(steps), template="orange")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}

    # 3) 直接建仓
    try:
        r = pps.import_plan_only(db, pgn)
        batch = db.get(Batch, r["batch_id"]) if r else None
        if batch is None:
            raise RuntimeError("采购计划落批次失败")
        inbound_service.build_for_batch(db, batch)
        try:
            opts = json.loads(batch.placement_options or "[]")
        except (ValueError, TypeError):
            opts = []
        workspace.record_plan(opname, pgn, stage="已建仓", note=f"{len(opts)} 个分仓方案")
        steps.append(f"✅ 建仓完成 → {len(opts)} 个分仓方案")
        # 采购+建仓进度已记入工作区；给运营直接看分仓方案卡片
        card = cards.placement_card(batch.name or _batch_name(db, batch.id), opts, batch.id)
        return {"card": card, "sent": _safe_send_card(chat_id, card),
                "action": "confirm_purchase", "batch_id": batch.id}
    except RuntimeError as e:
        db.rollback()
        workspace.log(opname, pgn, f"建仓失败：{e}", level="error", shop=shop)
        steps.append(f"⚠️ 采购已到待到货；**建仓失败**：{str(e)[:160]}")
        card = cards.text_card("确认采购完成 · 建仓待处理", "\n".join(steps), template="orange")
        card["elements"].append({"tag": "action", "actions": [
            _btn_dict("🏗️ 重试建仓", "build", {"plan_group_no": pgn}, "primary")]})
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "confirm_purchase"}


def _act_build(db, chat_id, open_id, value):
    """【(重试)建仓】→ 采购计划落批次 + build_for_batch → 分仓方案。写工作区。"""
    value = value or {}
    pgn = value.get("plan_group_no")
    batch_id = value.get("batch_id")
    op = intake.identify_operator(db, open_id=open_id)
    opname = (op.name if op else None) or "运营"
    from ..services import inbound_service
    from ..services import purchase_plan_service as pps
    try:
        if batch_id is None and pgn:
            r = pps.import_plan_only(db, pgn)
            if not r:
                card = cards.text_card("建仓", f"采购计划 {pgn} 未找到", template="red")
                return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "build"}
            batch_id = r["batch_id"]
        batch = db.get(Batch, batch_id)
        if batch is None:
            card = cards.text_card("建仓", "批次不存在", template="red")
            return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "build"}
        inbound_service.build_for_batch(db, batch)
    except RuntimeError as e:
        db.rollback()
        if pgn:
            workspace.log(opname, pgn, f"建仓失败：{e}", level="error")
        card = cards.text_card("建仓失败", str(e)[:220], template="red")
        return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "build"}
    try:
        opts = json.loads(batch.placement_options or "[]")
    except (ValueError, TypeError):
        opts = []
    if pgn:
        workspace.record_plan(opname, pgn, stage="已建仓", note=f"{len(opts)} 个分仓方案")
    lines = [f"✅ **{batch.name}** 建仓完成，生成 **{len(opts)}** 个分仓方案："]
    for i, o in enumerate(opts[:4], 1):
        shs = o.get("shipments") or []
        fcs = "、".join(f"{s.get('fc')}({s.get('boxes')}箱)" for s in shs[:8])
        lines.append(f"**方案{i}**（{o.get('label', '')}）：{len(shs)}仓　{fcs}")
    card = cards.text_card("建仓完成 · 分仓方案", "\n".join(lines), template="green")
    return {"card": card, "sent": _safe_send_card(chat_id, card), "action": "build",
            "batch_id": batch_id}


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
