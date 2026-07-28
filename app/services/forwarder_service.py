"""货代沟通服务（阶段0 管道）：企微外呼 + webhook 收消息落库。

阶段0 目标=能从系统发一条给货代、能收到货代回复并落库（ForwarderMessage）。
提取报价/比价/起草（Claude）属阶段1，另起 inquiry 流程。设计见 AGENT_FORWARDER.md。

qiweapi webhook 真实结构（2026-06-17 据实测 raw 定稿）：
    {"data": [ <事件1>, <事件2>... ], "code": 0, "msg": "成功"}
每个事件字段：
    cmd          15000=聊天消息（要的）；11001 登录态/2001 已读回执 等=系统事件（跳过）
    msgData.content   文本正文（"你好"）；moreDetail[].text 同
    senderId     发送方 id；senderName 名（常空）
    fromRoomId   群 id（0=非群/私聊）；isRoomNotice 1=群
    userId       本实例账号 id（=我方），senderId==userId 即自己回显→跳过
    msgServerId / msgUniqueIdentifier   消息 id（幂等）
    msgType      文本 0/2，富媒体另算；timestamp 秒
无 data 数组的（如 {"msg":"设置订阅成功!!"}）是平台控制响应，跳过。
"""

import json
from datetime import datetime

from .. import qiwe_client as qiwe
from ..models import Forwarder, ForwarderMessage

REAL_MSG_CMD = 15000        # 聊天消息事件（其余 cmd 为系统事件）


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
    """按企微来源匹配货代：优先群 room_id，其次外部联系人 id。匹配不到返回 None。

    同一群可能绑多条货代记录（多品牌共用一个货代群，如链条厂群绑 Zentop/Byane/RazEdg）——
    优先取**有进行中询价(收集中)**的那条，否则取第一条，避免消息归到没询价的兄弟记录上。
    """
    q = db.query(Forwarder)
    if room_id:
        rows = q.filter(Forwarder.qiwe_room_id == str(room_id)).all()
        if rows:
            if len(rows) > 1:
                from ..models import Inquiry
                for f in rows:
                    open_inq = (db.query(Inquiry)
                                .filter(Inquiry.status == "收集中",
                                        Inquiry.target_forwarder_ids.like(f"%{f.id}%"))
                                .order_by(Inquiry.id.desc()).first())
                    if open_inq and f.id in json.loads(open_inq.target_forwarder_ids or "[]"):
                        return f
            return rows[0]
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


def _one_event(db, ev):
    """处理单个事件：只收 cmd=15000 的真实聊天消息（非自己回显），其余返回 None。

    无正文的富媒体消息（图片/文件报价）**也落库**存 raw——图片链路的字段结构
    （base64RawData 还是需另调媒体下载接口）从未有过真实样本，先攒 raw 再定方案
    （OPENSOURCE_PLAN.md §4.5-I.3）。下游安全：归属对空正文走引用/单开放询价，
    自动提取只处理非空正文或已带 media 路径的 image。
    """
    if not isinstance(ev, dict):
        return None
    if ev.get("cmd") != REAL_MSG_CMD:
        return None                                  # 系统事件（登录态/已读回执等）
    msg_data = ev.get("msgData") or {}
    content = msg_data.get("content") or ""
    our_id = str(ev.get("userId") or "")
    sender = str(ev.get("senderId") or "")
    if sender and sender == our_id:
        return None                                  # 自己发的回显，跳过

    room_raw = ev.get("fromRoomId") or 0
    room_id = str(room_raw) if str(room_raw) not in ("", "0") else ""
    msg_id = str(ev.get("msgUniqueIdentifier") or ev.get("msgServerId") or "")
    if msg_id:
        dup = (db.query(ForwarderMessage)
               .filter(ForwarderMessage.qiwe_msg_id == msg_id).first())
        if dup:
            return {"stored": False, "reason": "duplicate", "message_id": dup.id}

    ts = datetime.now()
    try:
        n = int(ev.get("timestamp"))
        ts = datetime.fromtimestamp(n / 1000 if n > 1e12 else n)
    except (ValueError, TypeError, OSError):
        pass

    fwd = _match_forwarder(db, sender, room_id)
    msg = ForwarderMessage(
        forwarder_id=(fwd.id if fwd else None),
        direction="in", content=str(content),
        msg_type=str(ev.get("msgType") if ev.get("msgType") is not None else "text"),
        qiwe_msg_id=msg_id, raw=json.dumps(ev, ensure_ascii=False), ts=ts)
    db.add(msg)
    db.flush()
    return {"stored": True, "message_id": msg.id,
            "matched_forwarder_id": (fwd.id if fwd else None),
            "reason": "ok" if fwd else "unmatched_sender"}


def record_incoming(db, payload):
    """webhook 回调 → 解析 data 数组里的真实聊天消息，逐条落 in 流水（msgId 幂等）。

    返回 {stored, count, results}。系统事件/控制响应（无 data 数组）静默跳过。
    """
    payload = payload or {}
    events = payload.get("data")
    if not isinstance(events, list):
        return {"stored": False, "count": 0, "reason": "no_message_events"}
    results = []
    for ev in events:
        r = _one_event(db, ev)
        if r:
            results.append(r)
    db.commit()
    stored = [r for r in results if r.get("stored")]
    return {"stored": bool(stored), "count": len(stored), "results": results}
