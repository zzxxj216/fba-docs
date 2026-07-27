"""询价线服务（Track-A 能力层）——建询价→群发→归属→提取→报全校验→整包比价→选定。

设计见 AGENT_FORWARDER.md（v2）。半自动：起草/归属/提取/比价由 LLM+规则做，
发询价/选货代等关键决策卡人工确认。

接缝（CONTRACT_PARALLEL.md §2，飞书线按这些签名对接）：
    start_inquiry(db, batch_id) -> Inquiry
    send_inquiry(db, inquiry_id) -> dict
    get_comparison(db, inquiry_id) -> dict
    choose_forwarder(db, inquiry_id, quote_id)

安全红线（CONTRACT §4）：系统永不确认发货/付款/承诺货量；询价类可自动发，
议价/选定走人工。本模块只发"询价"草稿（人工确认后）与"追问缺仓"，不发任何承诺性内容。
"""

import json
import os
from datetime import datetime

from .. import llm_client
from ..models import (
    Batch, Brand, Forwarder, ForwarderMessage, Inquiry, InquiryQuote, QuoteLine,
)
from ..modules import quote_extractor, quote_extractor_llm
from . import forwarder_service

# 正则置信度低于此阈值 → 走 LLM 兜底
REGEX_CONFIDENCE_FLOOR = 0.5
# 整包总价默认月度报关费（找不到报价里的 customs_fee_monthly 时按 0 计，不臆造）
OPEN_INQUIRY_STATUSES = ("待发送", "已发送", "收集中")


# ---------------------------------------------------------------- 工具

def _jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def _build_lanes(db, batch):
    """从批次的 shipments（per 目的仓 FC）汇总 lanes 快照。

    返回 [{fc, boxes, volume_cbm, weight_kg, skus}]。建仓后这些是权威分仓结果。
    """
    lanes = []
    for sh in (batch.shipments or []):
        skus = [it.msku for it in (sh.items or []) if it.msku]
        lanes.append({
            "fc": sh.fc_code or "",
            "boxes": sh.carton_num,
            "volume_cbm": sh.total_volume,
            "weight_kg": sh.total_weight,
            "skus": skus,
        })
    return [l for l in lanes if l["fc"]]


def _gen_ref_code(db, batch):
    """生成人读暗号 INQ-{brand2}{MMDD}-{n}，埋进正文便于归属。"""
    brand2 = ""
    if batch.brand_id:
        brand = db.get(Brand, batch.brand_id)
        if brand:
            brand2 = (brand.abbr2 or brand.name[:3] or "").upper()
    tag = (brand2 or (batch.name or "BAT")[:3]).upper().replace(" ", "")
    today = datetime.now().strftime("%m%d")
    base = f"INQ-{tag}{today}"
    n = 1 + db.query(Inquiry).filter(Inquiry.ref_code.like(f"{base}-%")).count()
    return f"{base}-{n}"


def _brand_forwarders(db, brand_id):
    """品牌绑定的货代（active）；有 is_default 的排前。无绑定时退回全局默认货代。"""
    if brand_id:
        rows = (db.query(Forwarder)
                .filter(Forwarder.bind_brand_id == brand_id, Forwarder.active.is_(True))
                .all())
        if rows:
            rows.sort(key=lambda f: (not f.is_default, f.id))
            return rows
    return (db.query(Forwarder)
            .filter(Forwarder.is_default.is_(True), Forwarder.active.is_(True)).all())


# ---------------------------------------------------------------- 起草正文

def _draft_content(inquiry, lanes, batch, forwarders):
    """询价正文＝固定最简模板（2026-07-07 用户拍板）：
    "您好，这几个仓麻烦报一下价格" + 逐仓货量。不带暗号/编号、不列报价要素、不走 LLM。"""
    lanes_text = "\n".join(
        f"{l['fc']}：{l.get('boxes') or '?'}箱 / {l.get('volume_cbm') or '?'}方 / "
        f"{l.get('weight_kg') or '?'}kg"
        for l in lanes)
    return f"您好，这几个仓麻烦报一下价格：\n{lanes_text}"


def start_inquiry(db, batch_id):
    """建询价 + LLM 起草整包询价正文（status=待发送，待人工确认后 send）。

    接缝函数：飞书线据此发起询价。返回 Inquiry（含草稿 content + lanes_snapshot + ref_code）。
    """
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise ValueError(f"批次不存在：{batch_id}")
    lanes = _build_lanes(db, batch)
    if not lanes:
        raise ValueError("批次没有可询价的目的仓（请先完成建仓/分仓）")

    forwarders = _brand_forwarders(db, batch.brand_id)
    channel = "room" if any(f.qiwe_room_id for f in forwarders) else "private"

    inq = Inquiry(batch_id=batch_id, status="待发送", channel=channel,
                  lanes_snapshot=json.dumps(lanes, ensure_ascii=False),
                  structured=json.dumps({"lanes": lanes}, ensure_ascii=False),
                  target_forwarder_ids=json.dumps([f.id for f in forwarders]))
    db.add(inq)
    db.flush()
    inq.ref_code = _gen_ref_code(db, batch)
    inq.content = _draft_content(inq, lanes, batch, forwarders)
    db.commit()
    return inq


def start_weekly_inquiry(db, batches, lanes, forwarder_ids, content, ref_code):
    """整包询价：跨批次合并的 Inquiry（batch_id 取代表批次，lanes 为合并目的仓）。

    content 为整包询价正文（已含暗号 ref_code），target_forwarder_ids 为去重货代。
    structured.weekly=True 标记，便于比价时识别整包询价。
    """
    rep = batches[0]
    forwarders = [db.get(Forwarder, i) for i in forwarder_ids]
    channel = "room" if any(f and f.qiwe_room_id for f in forwarders) else "private"
    inq = Inquiry(batch_id=rep.id, status="待发送", channel=channel, ref_code=ref_code,
                  content=content,
                  lanes_snapshot=json.dumps(lanes, ensure_ascii=False),
                  structured=json.dumps({"lanes": lanes, "weekly": True,
                                         "batch_ids": [b.id for b in batches]},
                                        ensure_ascii=False),
                  target_forwarder_ids=json.dumps(list(forwarder_ids)))
    db.add(inq)
    db.commit()
    return inq


# ---------------------------------------------------------------- 发送

def send_inquiry(db, inquiry_id):
    """按品牌绑定的多家货代群发询价正文（走 qiwe），落 out 流水。

    接缝函数。安全：只发已定稿的询价正文（人工已在 UI 确认）。返回发送结果。
    """
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise ValueError(f"询价不存在：{inquiry_id}")
    if not (inq.content or "").strip():
        raise ValueError("询价正文为空，请先起草确认")
    ids = _jload(inq.target_forwarder_ids, []) or []
    forwarders = [db.get(Forwarder, i) for i in ids]
    forwarders = [f for f in forwarders if f is not None]
    if not forwarders:
        raise ValueError("本次询价没有目标货代（检查品牌货代绑定）")

    # 占坑：防两运营对同一批次重复外发询价（骚扰货代）；同人重发幂等放行
    from .. import coord_client as coord
    b = db.get(Batch, inq.batch_id) if inq.batch_id else None
    inq_scope = f"inqsend:{(b.purchase_plan_no or b.name) if b else inq.ref_code}"
    coord.claim(inq_scope, node="inqsend", refs={"ref_code": inq.ref_code})

    results = []
    for f in forwarders:
        try:
            msg = forwarder_service.send_message(
                db, f, inq.content, inquiry_id=inq.id, batch_id=inq.batch_id)
            results.append({"forwarder_id": f.id, "name": f.name,
                            "sent": True, "message_id": msg.id})
        except RuntimeError as e:
            results.append({"forwarder_id": f.id, "name": f.name,
                            "sent": False, "error": str(e)})
    if any(r["sent"] for r in results):
        inq.status = "收集中"
        db.commit()
    return {"inquiry_id": inq.id, "status": inq.status, "results": results}


# ---------------------------------------------------------------- 归属

def _open_inquiries_for_forwarder(db, forwarder_id):
    """某货代当前所有开放 Inquiry（其 target_forwarder_ids 含该货代）。"""
    rows = (db.query(Inquiry)
            .filter(Inquiry.status.in_(OPEN_INQUIRY_STATUSES))
            .order_by(Inquiry.id.desc()).all())
    out = []
    for inq in rows:
        ids = _jload(inq.target_forwarder_ids, []) or []
        if forwarder_id in ids:
            out.append(inq)
    return out


def _attribute_by_reply(db, msg):
    """引用优先：消息引用了我方某条 out → 顺其 inquiry_id 归属（最强信号）。"""
    if not msg.reply_to_msg_id:
        return None
    out = (db.query(ForwarderMessage)
           .filter(ForwarderMessage.qiwe_msg_id == msg.reply_to_msg_id,
                   ForwarderMessage.direction == "out").first())
    if out and out.inquiry_id:
        return out.inquiry_id
    return None


def _attribute_by_llm(db, msg, candidates):
    """多个开放 Inquiry → LLM 判别这条回复答的是哪个。

    返回 (inquiry_id|None, confidence)。缺 key/失败 → (None, 0)。
    """
    if not llm_client.available():
        return None, 0.0
    options = []
    for inq in candidates:
        lanes = _jload(inq.lanes_snapshot, []) or []
        fcs = [l.get("fc") for l in lanes if l.get("fc")]
        options.append({"inquiry_id": inq.id, "ref_code": inq.ref_code,
                        "fcs": fcs, "boxes": sum((l.get("boxes") or 0) for l in lanes)})
    try:
        data = llm_client.chat_json([
            {"role": "system", "content": "你判断货代这条回复是回应哪一个询价单，只输出 JSON。"},
            {"role": "user", "content":
                "候选询价（每个有目的仓FC/箱数/暗号）：\n" +
                json.dumps(options, ensure_ascii=False) +
                "\n\n货代回复原文：\n" + (msg.content or "") +
                '\n\n输出 {"inquiry_id":匹配的id或null,"confidence":0到1}。'
                "回复里若出现暗号或目的仓代码/箱数能对上则高置信。"}],
            max_tokens=200)
        iid = data.get("inquiry_id")
        conf = float(data.get("confidence") or 0)
        iid = int(iid) if iid not in (None, "", "null") else None
        if iid not in {o["inquiry_id"] for o in options}:
            iid = None
        return iid, conf
    except (llm_client.LLMUnavailable, ValueError, TypeError):
        return None, 0.0


def attribute_message(db, msg, llm_threshold=0.6):
    """把一条 in 消息归属到正确的 Inquiry。

    策略（AGENT_FORWARDER §3）：
      1. 引用优先（reply_to_msg_id）
      2. 该货代仅 1 个开放 Inquiry → 直接
      3. 多个开放 → ref_code 文本命中 → 再不行 LLM 判别（高置信自动，低/模糊转人工）
    写回 msg.inquiry_id / attribution_status。返回归属结果 dict。
    """
    if not msg.forwarder_id:
        msg.attribution_status = "none"
        db.commit()
        return {"inquiry_id": None, "status": "none", "reason": "unmatched_forwarder"}

    # 1) 引用
    iid = _attribute_by_reply(db, msg)
    if iid:
        msg.inquiry_id = iid
        msg.attribution_status = "auto"
        db.commit()
        return {"inquiry_id": iid, "status": "auto", "reason": "reply"}

    candidates = _open_inquiries_for_forwarder(db, msg.forwarder_id)
    if not candidates:
        msg.attribution_status = "none"
        db.commit()
        return {"inquiry_id": None, "status": "none", "reason": "no_open_inquiry"}

    # 2) 单开放
    if len(candidates) == 1:
        msg.inquiry_id = candidates[0].id
        msg.attribution_status = "auto"
        db.commit()
        return {"inquiry_id": candidates[0].id, "status": "auto", "reason": "single_open"}

    # 3a) ref_code 文本命中
    content = msg.content or ""
    for inq in candidates:
        if inq.ref_code and inq.ref_code in content:
            msg.inquiry_id = inq.id
            msg.attribution_status = "auto"
            db.commit()
            return {"inquiry_id": inq.id, "status": "auto", "reason": "ref_code"}

    # 3b) LLM 判别
    iid, conf = _attribute_by_llm(db, msg, candidates)
    if iid and conf >= llm_threshold:
        msg.inquiry_id = iid
        msg.attribution_status = "auto"
        db.commit()
        return {"inquiry_id": iid, "status": "auto", "reason": "llm", "confidence": conf}

    # 模糊 → 待人工
    msg.attribution_status = "pending"
    db.commit()
    return {"inquiry_id": None, "status": "pending", "reason": "ambiguous",
            "candidates": [c.id for c in candidates],
            "llm_guess": iid, "confidence": conf}


def assign_message(db, msg_id, inquiry_id):
    """人工指派：把待归属消息绑到指定 Inquiry。"""
    msg = db.get(ForwarderMessage, msg_id)
    if msg is None:
        raise ValueError(f"消息不存在：{msg_id}")
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise ValueError(f"询价不存在：{inquiry_id}")
    msg.inquiry_id = inquiry_id
    msg.attribution_status = "manual"
    db.commit()
    return {"message_id": msg_id, "inquiry_id": inquiry_id, "status": "manual"}


def record_incoming(db, payload):
    """webhook 收消息 → 落库（复用 forwarder_service）→ 对每条 in 消息跑归属。

    返回 forwarder_service.record_incoming 的结果 + 各消息归属结果。
    """
    res = forwarder_service.record_incoming(db, payload)
    attributed = []
    for r in res.get("results", []):
        if r.get("stored") and r.get("message_id"):
            msg = db.get(ForwarderMessage, r["message_id"])
            if msg and msg.direction == "in":
                a = attribute_message(db, msg)
                # 归属到某询价 → 自动提取报价（文字/图片）落 QuoteLine，免人工触发
                if msg.inquiry_id and a.get("status") in ("auto", "manual"):
                    try:
                        a["extracted"] = bool(_auto_extract(db, msg))
                    except Exception:
                        a["extracted"] = False
                attributed.append(a)
    res["attribution"] = attributed
    return res


def _conversation_text(db, inquiry_id, forwarder_id, limit=15):
    """该货代针对此询价的近期来信拼成一段，供 LLM 整体提取（货代常分多条发/改价）。"""
    rows = (db.query(ForwarderMessage)
            .filter(ForwarderMessage.forwarder_id == forwarder_id,
                    ForwarderMessage.inquiry_id == inquiry_id,
                    ForwarderMessage.direction == "in")
            .order_by(ForwarderMessage.id.asc()).all())
    parts = [(m.content or "").strip() for m in rows[-limit:] if (m.content or "").strip()]
    return "\n".join(parts)


def _auto_extract(db, msg):
    """归属成功的来信 → 自动提取报价（整段对话喂 LLM）→ 算缺仓/总价 + 提醒补报 + 通知运营。"""
    quote = None
    if msg.msg_type == "image":
        media = _jload(msg.media, {}) or {}
        path = media.get("path") or media.get("local_path") or ""
        if path and os.path.exists(path):
            quote = extract_quote_from_image(db, msg.inquiry_id, msg.forwarder_id, path)
    elif (msg.content or "").strip():
        # 逐条消息提取 + 按仓合并(_persist_quote)：货代分多条/补报时稳，不会覆盖已报
        quote = extract_quote_from_text(db, msg.inquiry_id, msg.forwarder_id, msg.content)
    # 只有这条消息真提取到报价行才通知/推比价卡——闲聊"收到/OK/稍等"(0行)不触发，避免刷屏
    if quote is not None and getattr(quote, "_extracted_lines", 0) > 0:
        try:
            _post_extract(db, msg.inquiry_id, msg.forwarder_id, quote)
        except Exception:
            pass
    return quote


def _post_extract(db, inquiry_id, forwarder_id, quote):
    """提取后：算缺仓 + 整包总价；缺仓则群里提醒货代补报；通知管该店铺的运营。"""
    inq = db.get(Inquiry, inquiry_id)
    lanes = _jload(inq.lanes_snapshot, []) if inq else []
    want = {(l.get("fc") or "").upper() for l in lanes if l.get("fc")}
    got = {(ln.fc or "").upper() for ln in quote.lines if (ln.fc or "") and ln.price is not None}
    flat = any((ln.fc or "").upper() in ("", "ALL") and ln.price is not None for ln in quote.lines)
    missing = [] if flat else sorted(want - got)
    total = currency = None
    try:
        pt = _package_total(quote, lanes)
        total, currency = pt.get("total"), pt.get("currency")
    except Exception:
        pass
    fwd = db.get(Forwarder, forwarder_id)
    if missing and got and fwd:            # 报了一部分但有缺 → 群里提醒补
        try:
            forwarder_service.send_message(
                db, fwd, "收到，谢谢！还差这几个仓的价格麻烦补一下：" + "、".join(missing),
                inquiry_id=inquiry_id)
        except Exception:
            pass
    if fwd:
        _notify_operator_quote(db, inq, fwd, total, currency or quote.currency,
                               len(got), len(want), missing)


def _notify_operator_quote(db, inq, fwd, total, currency, covered, want_n, missing):
    """货代回价后，通知管该店铺的运营（飞书私信）：覆盖/整包总价/缺仓。"""
    try:
        from ..feishu_models import Operator
        from .. import feishu_client as fc
        import json as _json
    except Exception:
        return
    b = db.get(Batch, inq.batch_id) if inq else None
    if not b:
        return
    target = None
    for o in db.query(Operator).all():
        try:
            sids = _json.loads(o.scope_brand_ids or "[]")
        except (ValueError, TypeError):
            sids = []
        if o.is_admin or (b.brand_id in sids):
            target = o
            break
    if not target or not target.feishu_open_id:
        return
    msg = (f"💰 {fwd.name} 回价：覆盖 {covered}/{want_n} 个仓"
           + (f"，整包约 {total} {currency}" if total is not None else "")
           + (f"；还缺 {('、'.join(missing))}（已自动提醒货代补报）" if missing
              else "；已报全 👇 直接按批次选货代"))
    try:
        fc.send_text(target.feishu_open_id, msg, receive_id_type="open_id")
    except Exception:
        pass
    # 仅当"本周所有整包询价都报全"时，才主动把按批次比价卡推给运营（不用手动发「比价」）
    if not missing and _weekly_all_complete(db, target):
        try:
            from ..feishu import service as fsvc, cards as fcards
            comp = (fsvc.weekly_comparison_from_db(db, target) or {}).get("comparison") or []
            if comp:
                fc.send_text(target.feishu_open_id, "✅ 本周所有询价都报全了，这是按批次比价，请逐批选货代：",
                             receive_id_type="open_id")
                fc.send_card(target.feishu_open_id, fcards.weekly_comparison_card(comp),
                             receive_id_type="open_id")
        except Exception:
            pass


def _weekly_all_complete(db, target_op):
    """该运营本周所有整包询价是否都已报全（每条询价至少有一家货代覆盖其全部仓）。"""
    import json as _json
    from datetime import datetime, timedelta
    now = datetime.now()
    ws = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        sids = _json.loads(target_op.scope_brand_ids or "[]")
    except (ValueError, TypeError):
        sids = []
    inqs = []
    for cand in db.query(Inquiry).filter(Inquiry.created_at >= ws).all():
        try:
            st = _json.loads(cand.structured or "{}")
        except (ValueError, TypeError):
            st = {}
        if not st.get("weekly"):
            continue
        if not target_op.is_admin:
            b = db.get(Batch, cand.batch_id)
            if not sids or (b and b.brand_id not in sids):
                continue
        inqs.append(cand)
    if not inqs:
        return False
    for inq in inqs:
        lanes = _jload(inq.lanes_snapshot, [])
        want = {(l.get("fc") or "").upper() for l in lanes if l.get("fc")}
        done = False
        for q in db.query(InquiryQuote).filter(InquiryQuote.inquiry_id == inq.id).all():
            got = {(ln.fc or "").upper() for ln in q.lines if (ln.fc or "") and ln.price is not None}
            flat = any((ln.fc or "").upper() in ("", "ALL") and ln.price is not None for ln in q.lines)
            if flat or want <= got:
                done = True
                break
        if not done:
            return False
    return True


# ---------------------------------------------------------------- 提取 → QuoteLine

def submit_structured_quote(db, inquiry_id, forwarder_id, payload):
    """结构化报价提交（Codex 在运营端提取后直接落库，绕过内置 LLM/正则）。

    payload: {lines:[{fc,price,unit?,channel?,eta_days?,cutoff?,min_charge?,remark?,currency?}],
              currency?, customs_fee_monthly?, raw_ref?(原文引用，留档)}。
    返回 {quote_id, lines, missing}——missing=询价 lanes 里还没报价的 FC，
    **不自动外发催报**（追问由用户在对话里确认后另发）。"""
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise ValueError(f"询价不存在：{inquiry_id}")
    if db.get(Forwarder, forwarder_id) is None:
        raise ValueError(f"货代不存在：{forwarder_id}")
    lines = payload.get("lines") or []
    if not lines:
        raise ValueError("缺少报价行 lines")
    extracted = {"lines": lines, "currency": payload.get("currency"),
                 "customs_fee_monthly": payload.get("customs_fee_monthly"),
                 "confidence": 1.0, "source_type": "manual"}
    quote = _persist_quote(db, inquiry_id, forwarder_id, extracted,
                           raw_message=(payload.get("raw_ref") or "")[:2000])
    lane_fcs = {(l.get("fc") or "").upper() for l in (_jload(inq.lanes_snapshot, []) or [])}
    quoted = {(ln.fc or "").upper() for ln in quote.lines if ln.price is not None}
    return {"quote_id": quote.id,
            "lines": [{"fc": ln.fc, "price": ln.price, "unit": ln.unit} for ln in quote.lines],
            "missing": sorted(lane_fcs - quoted - {""})}


def _persist_quote(db, inquiry_id, forwarder_id, extracted, raw_message="",
                   raw_image_path=""):
    """提取结果 dict → InquiryQuote + QuoteLine[]。**按仓合并(upsert)**：保留旧仓、更新/新增
    提取到的仓，支持货代分多条增量报价（如先报4仓、再补1仓），不会把已报的覆盖没。"""
    lines = extracted.get("lines") or []
    rep = next((l for l in lines if l.get("price") is not None), {})
    quote = (db.query(InquiryQuote)
             .filter(InquiryQuote.inquiry_id == inquiry_id,
                     InquiryQuote.forwarder_id == forwarder_id).first())
    if quote is None:
        quote = InquiryQuote(inquiry_id=inquiry_id, forwarder_id=forwarder_id)
        db.add(quote)
    quote.source_type = extracted.get("source_type", "text")
    if raw_message:
        quote.raw_message = ((quote.raw_message + "\n") if quote.raw_message else "") + raw_message
    if raw_image_path:
        quote.raw_image_path = raw_image_path
    if extracted.get("currency"):
        quote.currency = extracted["currency"]
    elif not quote.currency:
        quote.currency = "CNY"
    if rep.get("price") is not None:
        quote.price, quote.unit = rep.get("price"), rep.get("unit") or quote.unit
    if rep.get("channel"):
        quote.channel = rep["channel"]
    if rep.get("eta_days") is not None:
        quote.eta_days = rep["eta_days"]
    if rep.get("cutoff"):
        quote.cutoff = rep["cutoff"]
    if extracted.get("customs_fee_monthly") is not None:   # None 不覆盖已有报关费
        quote.customs_fee_monthly = extracted["customs_fee_monthly"]
    quote.extract_confidence = extracted.get("confidence")
    db.flush()
    # 按 FC upsert：更新已有仓、新增新仓；其余旧仓保留（增量合并）
    by_fc = {(ln.fc or "").upper(): ln for ln in quote.lines}
    for d in lines:
        fc = (d.get("fc") or "").upper()
        ln = by_fc.get(fc)
        if ln is None:
            ln = QuoteLine(fc=d.get("fc") or "")
            quote.lines.append(ln)
            by_fc[fc] = ln
        ln.price = d.get("price")
        ln.currency = d.get("currency") or quote.currency
        ln.unit = d.get("unit") or "/kg"
        ln.channel = d.get("channel") or ln.channel or ""
        ln.eta_days = d.get("eta_days") if d.get("eta_days") is not None else ln.eta_days
        ln.cutoff = d.get("cutoff") or ln.cutoff or ""
        ln.min_charge = d.get("min_charge") if d.get("min_charge") is not None else ln.min_charge
        if d.get("remark"):
            ln.remark = (d.get("remark") or "")[:255]
    db.commit()
    return quote


def extract_quote_from_text(db, inquiry_id, forwarder_id, text, use_llm_fallback=True):
    """文本报价提取：**优先大模型**(多仓不同价更准)，LLM 不可用/失败再退正则。落 QuoteLine。"""
    inq = db.get(Inquiry, inquiry_id)
    lanes = _jload(inq.lanes_snapshot, []) if inq else []
    extracted = None
    if use_llm_fallback and llm_client.available():
        try:
            llm_res = quote_extractor_llm.extract_text(text, lanes=lanes)
            if llm_res.get("lines"):
                extracted = llm_res
        except llm_client.LLMUnavailable:
            pass
    if extracted is None:                 # 退正则兜底
        extracted = quote_extractor.extract(text, lanes=lanes)
    quote = _persist_quote(db, inquiry_id, forwarder_id, extracted, raw_message=text)
    # 本条消息实际提取到的报价行数（闲聊"收到/OK"为0）→ 上层据此决定是否通知/推比价卡
    quote._extracted_lines = len([l for l in (extracted.get("lines") or [])
                                  if l.get("price") is not None])
    return quote


def extract_quote_from_image(db, inquiry_id, forwarder_id, image_path):
    """图片报价（视觉）提取，落 InquiryQuote(source_type=image)+QuoteLine。"""
    inq = db.get(Inquiry, inquiry_id)
    lanes = _jload(inq.lanes_snapshot, []) if inq else []
    extracted = quote_extractor_llm.extract_image(image_path, lanes=lanes)
    quote = _persist_quote(db, inquiry_id, forwarder_id, extracted, raw_image_path=image_path)
    quote._extracted_lines = len([l for l in (extracted.get("lines") or [])
                                  if l.get("price") is not None])
    return quote


# ---------------------------------------------------------------- 报全校验 + 比价

def _quote_lines_as_dicts(quote):
    return [{"fc": l.fc, "price": l.price, "unit": l.unit, "currency": l.currency,
             "channel": l.channel, "eta_days": l.eta_days, "cutoff": l.cutoff,
             "min_charge": l.min_charge, "remark": l.remark} for l in quote.lines]


def _lane_chargeable(lane):
    """某仓的计费量：默认按重量(kg)，无重量退体积(cbm)。返回 (qty, basis)。"""
    w = lane.get("weight_kg")
    if w:
        return float(w), "kg"
    v = lane.get("volume_cbm")
    if v:
        return float(v), "cbm"
    return 0.0, ""


def _package_total(quote, lanes):
    """整包总价 = Σ各仓运费 + 月度固定报关费。

    每仓运费 = 单价 × 计费量（按 unit 匹配 kg/cbm；/票按票数=1 计）；取 max(运费, min_charge)。
    返回 {total, currency, per_fc:[...], missing:[...], risks:[...]}。
    一口价(fc=ALL/空)应用到所有 lanes。
    """
    line_by_fc = {}
    flat = None
    for l in quote.lines:
        fc = (l.fc or "").upper()
        if fc in ("", "ALL"):
            flat = l
        else:
            line_by_fc[fc] = l

    per_fc = []
    risks = []
    missing = []
    total = 0.0
    currency = quote.currency or "CNY"
    for lane in lanes:
        fc = str(lane.get("fc")).upper()
        line = line_by_fc.get(fc) or flat
        if line is None or line.price is None:
            missing.append(fc)
            per_fc.append({"fc": fc, "fee": None, "missing": True})
            continue
        qty, basis = _lane_chargeable(lane)
        unit = (line.unit or "/kg").lower()
        if "票" in unit:
            fee = float(line.price)            # 一票一价
        elif "cbm" in unit or "方" in unit:
            v = lane.get("volume_cbm") or 0
            fee = float(line.price) * float(v)
            if not v:
                risks.append(f"{fc} 按方计价但缺体积")
        else:  # /kg 默认
            w = lane.get("weight_kg") or 0
            fee = float(line.price) * float(w)
            if not w:
                risks.append(f"{fc} 按kg计价但缺重量")
        if line.min_charge and fee < line.min_charge:
            fee = float(line.min_charge)
        per_fc.append({"fc": fc, "fee": round(fee, 2), "unit": line.unit,
                       "price": line.price, "via": "flat" if line is flat else "fc"})
        total += fee

    customs = quote.customs_fee_monthly or 0.0
    total += float(customs)
    return {"total": round(total, 2), "currency": currency, "customs_fee": customs,
            "freight_total": round(total - float(customs), 2),
            "per_fc": per_fc, "missing": missing, "risks": risks}


def coverage_for_quote(db, inquiry, quote):
    """单份报价对 lanes 的报全校验。"""
    lanes = _jload(inquiry.lanes_snapshot, []) or []
    return quote_extractor.coverage_check(lanes, _quote_lines_as_dicts(quote))


def get_comparison(db, inquiry_id):
    """整包比价：各货代整包总价（Σ各仓运费 + 月度报关费），排序 + 推荐 + 标缺仓/风险。

    接缝函数。返回 {inquiry_id, lanes, quotes:[...排序...], recommended_quote_id, ...}。
    """
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise ValueError(f"询价不存在：{inquiry_id}")
    lanes = _jload(inq.lanes_snapshot, []) or []
    quotes = (db.query(InquiryQuote)
              .filter(InquiryQuote.inquiry_id == inquiry_id).all())

    rows = []
    for q in quotes:
        cov = quote_extractor.coverage_check(lanes, _quote_lines_as_dicts(q))
        pkg = _package_total(q, lanes)
        fwd = db.get(Forwarder, q.forwarder_id) if q.forwarder_id else None
        rows.append({
            "quote_id": q.id,
            "forwarder_id": q.forwarder_id,
            "forwarder_name": fwd.name if fwd else "",
            "source_type": q.source_type,
            "currency": pkg["currency"],
            "package_total": pkg["total"],
            "freight_total": pkg["freight_total"],
            "customs_fee": pkg["customs_fee"],
            "per_fc": pkg["per_fc"],
            "complete": cov["complete"] and not pkg["missing"],
            "missing": sorted(set(cov["missing"]) | set(pkg["missing"])),
            "extra": cov["extra"],
            "flat_rate": cov["flat_rate"],
            "risks": pkg["risks"],
            "confidence": q.extract_confidence,
            "is_chosen": q.is_chosen,
            "lines": _quote_lines_as_dicts(q),
        })

    # 排序：先报全的，再总价低的（缺仓的总价不可比，排后）
    def _key(r):
        return (0 if r["complete"] else 1,
                r["package_total"] if r["package_total"] is not None else float("inf"))
    rows.sort(key=_key)

    recommended = None
    reason = ""
    complete_rows = [r for r in rows if r["complete"] and r["package_total"]]
    if complete_rows:
        best = complete_rows[0]
        recommended = best["quote_id"]
        reason = (f"{best['forwarder_name'] or '该货代'} 报全{len(lanes)}仓且整包总价最低"
                  f"（{best['package_total']} {best['currency']}）")
        if best["risks"]:
            reason += f"；注意：{'；'.join(best['risks'])}"
    elif rows:
        reason = "暂无报全的报价，建议追问缺仓后再比"

    return {
        "inquiry_id": inquiry_id,
        "ref_code": inq.ref_code,
        "status": inq.status,
        "lanes": lanes,
        "lane_count": len(lanes),
        "quotes": rows,
        "recommended_quote_id": recommended,
        "recommendation": reason,
    }


# ---------------------------------------------------------------- 追问 + 选定

def followup_missing(db, inquiry_id, quote_id):
    """对某货代漏报的仓生成追问文案（询价类，可自动发；这里只返回草稿，发送由上层决定）。"""
    inq = db.get(Inquiry, inquiry_id)
    quote = db.get(InquiryQuote, quote_id)
    if inq is None or quote is None:
        raise ValueError("询价或报价不存在")
    cov = coverage_for_quote(db, inq, quote)
    if not cov["missing"]:
        return {"missing": [], "draft": ""}
    lanes = {str(l.get("fc")).upper(): l for l in (_jload(inq.lanes_snapshot, []) or [])}
    detail = "、".join(
        f"{fc}（{lanes.get(fc, {}).get('boxes', '?')}箱）" for fc in cov["missing"])
    draft = (f"{inq.ref_code} 麻烦您再补报这几个仓的头程：{detail}，"
             "运费单价/渠道/时效/截关即可，多谢！")
    return {"missing": cov["missing"], "draft": draft}


def choose_forwarder(db, inquiry_id, quote_id):
    """人工选定货代（拍板）。安全：仅标记选定，不发任何确认/承诺给货代。

    接缝函数。返回 {inquiry_id, chosen_quote_id, status}。
    """
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise ValueError(f"询价不存在：{inquiry_id}")
    quote = db.get(InquiryQuote, quote_id)
    if quote is None or quote.inquiry_id != inquiry_id:
        raise ValueError("报价不存在或不属于该询价")
    for q in db.query(InquiryQuote).filter(InquiryQuote.inquiry_id == inquiry_id).all():
        q.is_chosen = (q.id == quote_id)
    inq.chosen_quote_id = quote_id
    inq.status = "已选货代"
    db.commit()
    return {"inquiry_id": inquiry_id, "chosen_quote_id": quote_id, "status": inq.status,
            "forwarder_id": quote.forwarder_id}
