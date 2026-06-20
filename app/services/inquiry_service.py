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
    """LLM 起草整包询价正文；缺 key 时退确定性模板。正文埋 ref_code 便于归属。"""
    total_boxes = sum((l.get("boxes") or 0) for l in lanes)
    fcs = [l["fc"] for l in lanes]
    country = (batch.country or "US") if batch else "US"
    summary = (f"【询价 {inquiry.ref_code}｜{country} {len(fcs)}仓/{total_boxes}箱】")
    lanes_text = "\n".join(
        f"  {l['fc']}：{l.get('boxes') or '?'}箱 / {l.get('volume_cbm') or '?'}方 / "
        f"{l.get('weight_kg') or '?'}kg"
        for l in lanes)
    fallback = (
        f"{summary}\n您好，有一批 FBA 头程货需要询价，目的仓及货量如下：\n"
        f"{lanes_text}\n"
        "请按每个目的仓分别报：运费单价(币种/单位)、渠道、时效、截关、起送费，"
        "另请告知月度报关费。多谢！\n"
        f"（回复请带暗号 {inquiry.ref_code} 便于我方对单）")

    if not llm_client.available():
        return fallback
    try:
        prompt = (
            "你是外贸跟单，给货代起草一条 FBA 头程整包询价微信消息。要求：礼貌专业、简洁，"
            "逐目的仓列出货量，明确请对方逐仓报价（运费单价/币种/单位/渠道/时效/截关/起送费）"
            "并问月度报关费。务必在开头保留这个暗号标题原样：\n" + summary +
            "\n并在结尾提示回复带上暗号 " + inquiry.ref_code + "。\n\n"
            "目的仓与货量：\n" + lanes_text +
            "\n\n安全红线：只询价，绝不承诺货量/价格/下单/合作，不要替我方做任何确认。只输出消息正文。")
        text = llm_client.chat(
            [{"role": "system", "content": "你只输出询价消息正文，不加解释。"},
             {"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.3)
        text = (text or "").strip()
        if text and inquiry.ref_code in text:
            return text
        if text:
            return f"{summary}\n{text}\n（回复请带暗号 {inquiry.ref_code}）"
    except llm_client.LLMUnavailable:
        pass
    return fallback


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


def _auto_extract(db, msg):
    """归属成功的来信 → 自动提取报价。文字走文本提取；图片有本地路径才走视觉提取。"""
    if msg.msg_type == "image":
        media = _jload(msg.media, {}) or {}
        path = media.get("path") or media.get("local_path") or ""
        if path and os.path.exists(path):
            return extract_quote_from_image(db, msg.inquiry_id, msg.forwarder_id, path)
        return None
    if (msg.content or "").strip():
        return extract_quote_from_text(db, msg.inquiry_id, msg.forwarder_id, msg.content)
    return None


# ---------------------------------------------------------------- 提取 → QuoteLine

def _persist_quote(db, inquiry_id, forwarder_id, extracted, raw_message="",
                   raw_image_path=""):
    """提取结果 dict → InquiryQuote + QuoteLine[]。同一询价同一货代已有则更新（覆盖行）。"""
    lines = extracted.get("lines") or []
    rep = next((l for l in lines if l.get("price") is not None), {})
    quote = (db.query(InquiryQuote)
             .filter(InquiryQuote.inquiry_id == inquiry_id,
                     InquiryQuote.forwarder_id == forwarder_id).first())
    if quote is None:
        quote = InquiryQuote(inquiry_id=inquiry_id, forwarder_id=forwarder_id)
        db.add(quote)
    quote.source_type = extracted.get("source_type", "text")
    quote.raw_message = raw_message or quote.raw_message
    quote.raw_image_path = raw_image_path or quote.raw_image_path
    quote.currency = extracted.get("currency") or "CNY"
    quote.price = rep.get("price")
    quote.unit = rep.get("unit") or ""
    quote.channel = rep.get("channel") or ""
    quote.eta_days = rep.get("eta_days")
    quote.cutoff = rep.get("cutoff") or ""
    quote.customs_fee_monthly = extracted.get("customs_fee_monthly")
    quote.extract_confidence = extracted.get("confidence")
    db.flush()
    # 重建行（用关系集合，保证 quote.lines 在 commit 后仍同步）
    quote.lines.clear()
    db.flush()
    for ln in lines:
        quote.lines.append(QuoteLine(
            fc=ln.get("fc") or "", price=ln.get("price"),
            currency=ln.get("currency") or quote.currency,
            unit=ln.get("unit") or "/kg", channel=ln.get("channel") or "",
            eta_days=ln.get("eta_days"), cutoff=ln.get("cutoff") or "",
            min_charge=ln.get("min_charge"), remark=(ln.get("remark") or "")[:255]))
    db.commit()
    return quote


def extract_quote_from_text(db, inquiry_id, forwarder_id, text, use_llm_fallback=True):
    """文本报价提取：先正则，置信度低且允许时走 LLM 兜底，落 InquiryQuote+QuoteLine。"""
    inq = db.get(Inquiry, inquiry_id)
    lanes = _jload(inq.lanes_snapshot, []) if inq else []
    extracted = quote_extractor.extract(text, lanes=lanes)
    if (extracted.get("confidence", 0) < REGEX_CONFIDENCE_FLOOR and use_llm_fallback
            and llm_client.available()):
        try:
            llm_res = quote_extractor_llm.extract_text(text, lanes=lanes)
            if llm_res.get("lines"):
                extracted = llm_res
        except llm_client.LLMUnavailable:
            pass
    return _persist_quote(db, inquiry_id, forwarder_id, extracted, raw_message=text)


def extract_quote_from_image(db, inquiry_id, forwarder_id, image_path):
    """图片报价（视觉）提取，落 InquiryQuote(source_type=image)+QuoteLine。"""
    inq = db.get(Inquiry, inquiry_id)
    lanes = _jload(inq.lanes_snapshot, []) if inq else []
    extracted = quote_extractor_llm.extract_image(image_path, lanes=lanes)
    return _persist_quote(db, inquiry_id, forwarder_id, extracted,
                          raw_image_path=image_path)


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
