"""询价线 REST 路由：起草→发→收(归属)→提取→报全→比价→选。

接缝（CONTRACT §2）：飞书卡片按钮走这些 REST。所有动作半自动——发询价/选货代
对应人工确认点（前端先看草稿/比价表再点）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ForwarderMessage, Inquiry, InquiryQuote
from ..services import inquiry_service as inq_svc

router = APIRouter()


def _inq_dict(inq):
    import json
    def _j(s):
        try:
            return json.loads(s) if s else None
        except (ValueError, TypeError):
            return None
    return {"id": inq.id, "batch_id": inq.batch_id, "ref_code": inq.ref_code,
            "status": inq.status, "channel": inq.channel, "content": inq.content,
            "lanes": _j(inq.lanes_snapshot), "chosen_quote_id": inq.chosen_quote_id,
            "target_forwarder_ids": _j(inq.target_forwarder_ids)}


@router.post("/inquiries")
def create_inquiry(data: dict, db: Session = Depends(get_db)):
    """起草询价（status=待发送，含草稿正文）。body {batch_id}。"""
    batch_id = (data or {}).get("batch_id")
    if not batch_id:
        raise HTTPException(400, "缺少 batch_id")
    try:
        inq = inq_svc.start_inquiry(db, batch_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _inq_dict(inq)


@router.get("/inquiries/{inquiry_id}")
def get_inquiry(inquiry_id: int, db: Session = Depends(get_db)):
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(404, "询价不存在")
    return _inq_dict(inq)


@router.put("/inquiries/{inquiry_id}/content")
def edit_content(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """人工改定稿正文（确认前编辑草稿）。"""
    inq = db.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(404, "询价不存在")
    inq.content = (data or {}).get("content", inq.content)
    db.commit()
    return _inq_dict(inq)


@router.post("/inquiries/{inquiry_id}/send")
def send_inquiry(inquiry_id: int, db: Session = Depends(get_db)):
    """人工确认后群发各货代（接缝 send_inquiry）。"""
    try:
        return inq_svc.send_inquiry(db, inquiry_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/inquiries/{inquiry_id}/comparison")
def comparison(inquiry_id: int, db: Session = Depends(get_db)):
    """整包比价 + 推荐（接缝 get_comparison）。"""
    try:
        return inq_svc.get_comparison(db, inquiry_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/inquiries/{inquiry_id}/choose")
def choose(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """人工选定货代（接缝 choose_forwarder）。body {quote_id}。"""
    quote_id = (data or {}).get("quote_id")
    if not quote_id:
        raise HTTPException(400, "缺少 quote_id")
    try:
        return inq_svc.choose_forwarder(db, inquiry_id, quote_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/inquiries/{inquiry_id}/extract-text")
def extract_text(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """从文本报价提取成 QuoteLine（body {forwarder_id, text}）。"""
    data = data or {}
    fid, text = data.get("forwarder_id"), data.get("text")
    if not fid or not text:
        raise HTTPException(400, "缺少 forwarder_id / text")
    quote = inq_svc.extract_quote_from_text(db, inquiry_id, fid, text)
    return {"quote_id": quote.id, "confidence": quote.extract_confidence,
            "lines": len(quote.lines)}


@router.post("/inquiries/{inquiry_id}/extract-image")
def extract_image(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """从图片报价（视觉）提取（body {forwarder_id, image_path}）。"""
    data = data or {}
    fid, path = data.get("forwarder_id"), data.get("image_path")
    if not fid or not path:
        raise HTTPException(400, "缺少 forwarder_id / image_path")
    try:
        quote = inq_svc.extract_quote_from_image(db, inquiry_id, fid, path)
    except FileNotFoundError:
        raise HTTPException(404, "图片不存在")
    except Exception as e:
        raise HTTPException(502, f"图片提取失败：{e}")
    return {"quote_id": quote.id, "confidence": quote.extract_confidence,
            "lines": len(quote.lines)}


@router.get("/inquiries/{inquiry_id}/coverage")
def coverage(inquiry_id: int, quote_id: int, db: Session = Depends(get_db)):
    """单份报价的报全校验。"""
    inq = db.get(Inquiry, inquiry_id)
    quote = db.get(InquiryQuote, quote_id)
    if inq is None or quote is None:
        raise HTTPException(404, "询价或报价不存在")
    return inq_svc.coverage_for_quote(db, inq, quote)


@router.post("/inquiries/{inquiry_id}/followup")
def followup(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """生成缺仓追问草稿（询价类，可发）。body {quote_id}。"""
    quote_id = (data or {}).get("quote_id")
    if not quote_id:
        raise HTTPException(400, "缺少 quote_id")
    try:
        return inq_svc.followup_missing(db, inquiry_id, quote_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/inquiries/messages/{msg_id}/assign")
def assign(msg_id: int, data: dict, db: Session = Depends(get_db)):
    """人工把待归属消息指派到某询价。body {inquiry_id}。"""
    inquiry_id = (data or {}).get("inquiry_id")
    if not inquiry_id:
        raise HTTPException(400, "缺少 inquiry_id")
    try:
        return inq_svc.assign_message(db, msg_id, inquiry_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/inquiries/{inquiry_id}/quotes")
def submit_quotes(inquiry_id: int, data: dict, db: Session = Depends(get_db)):
    """结构化报价提交（Codex 提取后直接落库，绕过内置 LLM/正则；按 FC 增量合并）。

    body {forwarder_id, lines:[{fc,price,unit?,channel?,eta_days?,cutoff?,min_charge?,
    remark?}], currency?, customs_fee_monthly?, raw_ref?}。返回含 missing 缺仓清单，
    不自动外发催报。"""
    data = data or {}
    fid = data.get("forwarder_id")
    if not fid:
        raise HTTPException(400, "缺少 forwarder_id")
    try:
        return inq_svc.submit_structured_quote(db, inquiry_id, int(fid), data)
    except ValueError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@router.get("/inquiries/messages/pending")
def pending_messages(db: Session = Depends(get_db)):
    """待人工归属的消息（attribution_status=pending）。"""
    rows = (db.query(ForwarderMessage)
            .filter(ForwarderMessage.attribution_status == "pending")
            .order_by(ForwarderMessage.ts.desc()).all())
    return [{"id": m.id, "forwarder_id": m.forwarder_id, "content": m.content,
             "ts": m.ts.isoformat() if m.ts else None} for m in rows]
