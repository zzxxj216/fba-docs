"""企微货代渠道路由：webhook 收消息 + 手动测试发送 + 渠道状态 + 消息流水查看。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Forwarder, ForwarderMessage
from ..services import forwarder_service as fs

router = APIRouter()


@router.get("/qiwe/status")
def qiwe_status():
    """渠道是否接通（前端据此提示"未配置 QIWE_TOKEN"）。"""
    return fs.channel_status()


@router.get("/qiwe/rooms")
def qiwe_rooms():
    """企微群列表（配货代群绑定时选 roomId）。"""
    from .. import qiwe_client as qiwe
    if not qiwe.configured():
        raise HTTPException(400, "企微渠道未配置（缺 QIWE_TOKEN）")
    try:
        rooms = qiwe.list_rooms()
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return [{"room_id": r.get("roomId"), "name": r.get("roomName"),
             "owner_id": r.get("roomOwnerId"), "member_count": r.get("roomMemberCount")}
            for r in rooms]


@router.post("/qiwe/callback")
async def qiwe_callback(request: Request, db: Session = Depends(get_db)):
    """qiweapi webhook 回调：货代消息进来 → 落库。容错解析，整段存 raw。

    平台可能用 GET 做校验、POST 推消息；这里只认 POST 的 JSON。
    始终回 {"code":0} 让平台认为已签收（避免重推风暴）。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        res = fs.record_incoming(db, payload)
    except Exception as e:               # 落库失败也别让平台疯狂重推
        return {"code": 0, "stored": False, "error": str(e)[:200]}
    return {"code": 0, **res}


@router.post("/qiwe/send-test")
def qiwe_send_test(data: dict, db: Session = Depends(get_db)):
    """手动测试发送：body {forwarder_id, content}。阶段0 验证管道用。"""
    data = data or {}
    fwd = db.get(Forwarder, data.get("forwarder_id"))
    if fwd is None:
        raise HTTPException(404, "货代不存在")
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        msg = fs.send_message(db, fwd, content)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"sent": True, "message_id": msg.id, "qiwe_msg_id": msg.qiwe_msg_id}


@router.get("/forwarders/{forwarder_id}/messages")
def forwarder_messages(forwarder_id: int, db: Session = Depends(get_db)):
    """某货代的沟通流水（时间正序）。"""
    rows = (db.query(ForwarderMessage)
            .filter(ForwarderMessage.forwarder_id == forwarder_id)
            .order_by(ForwarderMessage.ts.asc(), ForwarderMessage.id.asc()).all())
    return [{"id": m.id, "direction": m.direction, "content": m.content,
             "msg_type": m.msg_type, "inquiry_id": m.inquiry_id,
             "batch_id": m.batch_id, "ts": m.ts.isoformat() if m.ts else None}
            for m in rows]
