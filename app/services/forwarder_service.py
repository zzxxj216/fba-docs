"""货代沟通服务（阶段0 管道）：企微外呼 + webhook 收消息落库。

阶段0 目标=能从系统发一条给货代、能收到货代回复并落库（ForwarderMessage）。
提取报价/比价/起草（Claude）属阶段1，另起 inquiry 流程。设计见 AGENT_FORWARDER.md。

webhook 真实 payload 字段名未最终确认 —— 这里做容错解析（多候选键名）并整段存 raw，
见到真实回调后据 raw 收紧 _pick/_match。
"""

import json
from datetime import datetime

from .. import qiwe_client as qiwe
from ..models import Forwarder, ForwarderMessage


def _pick(d, *keys):
    """从 dict 按多个候选键取第一个非空值（容错不同 payload 命名）。"""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def channel_status():
    return {"configured": qiwe.configured(), "default_guid": qiwe.default_guid(),
            "base": qiwe._base()}


def _match_forwarder(db, sender_id, room_id):
    """按企微来源匹配货代：优先群 room_id，其次外部联系人 id。匹配不到返回 None。"""
    q = db.query(Forwarder)
    if room_id:
        f = q.filter(Forwarder.qiwe_room_id == str(room_id)).first()
        if f:
            return f
    if sender_id:
        return q.filter(Forwarder.qiwe_external_userid == str(sender_id)).first()
    return None


def send_message(db, forwarder, content, inquiry_id=None, batch_id=None, no_read=False):
    """向货代发文本（群优先），并落 out 流水。返回 ForwarderMessage。"""
    if not qiwe.configured():
        raise RuntimeError("企微渠道未配置（缺 QIWE_TOKEN）")
    to_id = forwarder.qiwe_room_id or forwarder.qiwe_external_userid
    if not to_id:
        raise RuntimeError(f"货代「{forwarder.name}」未配企微联系方式（qiwe_room_id / qiwe_external_userid）")
    resp = qiwe.send_text(to_id, content, guid=forwarder.qiwe_guid or None, no_read=no_read)
    msg = ForwarderMessage(
        forwarder_id=forwarder.id, inquiry_id=inquiry_id, batch_id=batch_id,
        direction="out", content=content, msg_type="text",
        qiwe_msg_id=str(_pick(resp or {}, "msgServerId", "msgUniqueIdentifier") or ""),
        raw=json.dumps(resp, ensure_ascii=False) if resp is not None else None)
    db.add(msg)
    db.commit()
    return msg


def record_incoming(db, payload):
    """webhook 收到货代消息 → 容错解析 + 落 in 流水（按 qiwe_msg_id 幂等）。

    返回 {stored: bool, matched_forwarder_id, message_id, reason}。
    解析不出发送方/内容也照存 raw，便于据真实回调迭代。
    """
    payload = payload or {}
    # 常见外层包装：{event/type, data:{...}} 或直接平铺
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    msg_id = _pick(data, "msgId", "msgServerId", "msgUniqueIdentifier", "id")
    if msg_id is not None:
        dup = (db.query(ForwarderMessage)
               .filter(ForwarderMessage.qiwe_msg_id == str(msg_id)).first())
        if dup:
            return {"stored": False, "reason": "duplicate", "message_id": dup.id}

    sender = _pick(data, "fromUser", "from", "sender", "externalUserId", "told", "userId")
    room_id = _pick(data, "roomId", "room", "chatId")
    content = _pick(data, "content", "text", "msg", "message") or ""
    msg_type = str(_pick(data, "msgType", "type") or "text")
    ts_raw = _pick(data, "timestamp", "ts", "createTime", "sendTime")
    ts = datetime.now()
    if ts_raw:
        try:
            n = int(ts_raw)
            ts = datetime.fromtimestamp(n / 1000 if n > 1e12 else n)
        except (ValueError, TypeError, OSError):
            ts = datetime.now()

    fwd = _match_forwarder(db, sender, room_id)
    msg = ForwarderMessage(
        forwarder_id=(fwd.id if fwd else None),
        direction="in", content=str(content), msg_type=msg_type,
        qiwe_msg_id=str(msg_id) if msg_id is not None else "",
        raw=json.dumps(payload, ensure_ascii=False), ts=ts)
    db.add(msg)
    db.commit()
    return {"stored": True, "matched_forwarder_id": (fwd.id if fwd else None),
            "message_id": msg.id,
            "reason": "ok" if fwd else "unmatched_sender"}
