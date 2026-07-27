"""企微货代渠道路由：webhook 收消息 + 手动测试发送 + 渠道状态 + 消息流水查看。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Forwarder, ForwarderMessage
from ..services import forwarder_service as fs
from ..services import inquiry_service

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
        # 用 inquiry_service：落库 + 对每条 in 消息自动归属到对的询价（多批次隔离）
        res = inquiry_service.record_incoming(db, payload)
    except Exception as e:               # 落库失败也别让平台疯狂重推
        return {"code": 0, "stored": False, "error": str(e)[:200]}
    return {"code": 0, **res}


@router.post("/qiwe/pull-relay")
def qiwe_pull_relay(db: Session = Depends(get_db)):
    """从 mcapi 中继拉取新消息导入本地（运营端形态，需 MCAPI_KEY）。

    原样事件走既有 record_incoming 管道（qiwe_msg_id 幂等去重 + 归属），
    游标存 output/_relay_cursor.json。"""
    import json as _json
    import os

    import httpx

    from ..amazon_fba_client import _base, _key
    from ..database import OUTPUT_DIR
    if not _key():
        raise HTTPException(400, "未配置 MCAPI_KEY（中继拉取是新架构运营端功能）")
    cur_path = os.path.join(OUTPUT_DIR, "_relay_cursor.json")
    since = 0
    try:
        with open(cur_path, encoding="utf-8") as f:
            since = int((_json.load(f) or {}).get("since_id") or 0)
    except (OSError, ValueError):
        pass
    try:
        r = httpx.get(f"{_base()}/api/v1/qiwe/relay/messages",
                      params={"since_id": since, "limit": 200},
                      headers={"X-API-Key": _key()}, timeout=60)
        rows = ((r.json() or {}).get("data") or []) if r.status_code < 400 else None
        if rows is None:
            raise HTTPException(502, f"中继拉取失败 HTTP {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"中继(mcapi)不可达：{e}")
    events = []
    for m in rows:
        try:
            events.append(_json.loads(m.get("raw") or "{}"))
        except ValueError:
            continue
    res = inquiry_service.record_incoming(db, {"data": events}) if events else {"count": 0}
    if rows:
        with open(cur_path, "w", encoding="utf-8") as f:
            _json.dump({"since_id": max(m["id"] for m in rows)}, f)
    return {"pulled": len(rows), "since_id": since, **res}


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
