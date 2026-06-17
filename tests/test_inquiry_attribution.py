"""归属隔离测试：引用优先 / 单开放直接 / 多开放 ref_code / 多开放 LLM 判别 / 模糊转人工。"""

import json

from app import llm_client
from app.models import ForwarderMessage, Inquiry
from app.services import inquiry_service as svc
from conftest import make_batch, make_forwarder


def _make_inquiry(db, batch_id, fwd_id, ref, fcs, status="收集中"):
    inq = Inquiry(batch_id=batch_id, status=status, ref_code=ref,
                  lanes_snapshot=json.dumps([{"fc": f, "boxes": 10} for f in fcs]),
                  target_forwarder_ids=json.dumps([fwd_id]))
    db.add(inq); db.commit()
    return inq


def _in_msg(db, fwd_id, content, reply_to=""):
    m = ForwarderMessage(forwarder_id=fwd_id, direction="in", content=content,
                         reply_to_msg_id=reply_to, qiwe_msg_id=f"m{content[:4]}")
    db.add(m); db.commit()
    return m


def test_attribute_single_open(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 10, 80, 1)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    msg = _in_msg(db, f.id, "ONT8 18块一公斤")
    res = svc.attribute_message(db, msg)
    assert res["status"] == "auto"
    assert res["reason"] == "single_open"
    assert msg.inquiry_id == inq.id


def test_attribute_by_reply(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 10, 80, 1), ("LAX9", 5, 40, 0.5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq1 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    inq2 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-2", ["LAX9"])
    # 我方对 inq2 发过一条 out
    out = ForwarderMessage(forwarder_id=f.id, inquiry_id=inq2.id, direction="out",
                           content="询价2", qiwe_msg_id="OUT-2")
    db.add(out); db.commit()
    # 货代引用 OUT-2 回复
    msg = _in_msg(db, f.id, "好的报你", reply_to="OUT-2")
    res = svc.attribute_message(db, msg)
    assert res["status"] == "auto" and res["reason"] == "reply"
    assert msg.inquiry_id == inq2.id


def test_attribute_by_ref_code(db, monkeypatch):
    """多开放，回复带暗号 → ref_code 命中，不需 LLM。"""
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 10, 80, 1), ("LAX9", 5, 40, 0.5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    inq2 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-2", ["LAX9"])
    msg = _in_msg(db, f.id, "INQ-HU0619-2 这个仓 20/kg")
    res = svc.attribute_message(db, msg)
    assert res["status"] == "auto" and res["reason"] == "ref_code"
    assert msg.inquiry_id == inq2.id


def test_attribute_multi_open_llm_high_conf(db, monkeypatch):
    """多开放、无暗号无引用 → LLM 判别，高置信自动归属。"""
    monkeypatch.setattr(llm_client, "available", lambda: True)
    batch = make_batch(db, [("ONT8", 10, 80, 1), ("LAX9", 5, 40, 0.5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq1 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    inq2 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-2", ["LAX9"])
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **k: {"inquiry_id": inq2.id, "confidence": 0.9})
    msg = _in_msg(db, f.id, "那个加州的仓我报 20/kg")
    res = svc.attribute_message(db, msg)
    assert res["status"] == "auto" and res["reason"] == "llm"
    assert msg.inquiry_id == inq2.id


def test_attribute_multi_open_llm_low_conf_pending(db, monkeypatch):
    """多开放、LLM 低置信 → 转人工 pending。"""
    monkeypatch.setattr(llm_client, "available", lambda: True)
    batch = make_batch(db, [("ONT8", 10, 80, 1), ("LAX9", 5, 40, 0.5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq1 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    inq2 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-2", ["LAX9"])
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **k: {"inquiry_id": inq1.id, "confidence": 0.3})
    msg = _in_msg(db, f.id, "稍等我算下")
    res = svc.attribute_message(db, msg)
    assert res["status"] == "pending" and res["reason"] == "ambiguous"
    assert msg.inquiry_id is None
    assert msg.attribution_status == "pending"
    assert set(res["candidates"]) == {inq1.id, inq2.id}


def test_attribute_no_forwarder_none(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    msg = ForwarderMessage(direction="in", content="未知来源", qiwe_msg_id="x1")
    db.add(msg); db.commit()
    res = svc.attribute_message(db, msg)
    assert res["status"] == "none"


def test_assign_message_manual(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: True)
    batch = make_batch(db, [("ONT8", 10, 80, 1)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq1 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    inq2 = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-2", ["LAX9"])
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **k: {"inquiry_id": None, "confidence": 0.2})
    msg = _in_msg(db, f.id, "模糊回复")
    svc.attribute_message(db, msg)
    assert msg.attribution_status == "pending"
    # 人工指派到 inq1
    res = svc.assign_message(db, msg.id, inq1.id)
    assert res["status"] == "manual"
    assert db.get(ForwarderMessage, msg.id).inquiry_id == inq1.id


def test_record_incoming_runs_attribution(db, monkeypatch):
    """webhook → 落库 + 自动归属（单开放）。"""
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 10, 80, 1)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="100")
    inq = _make_inquiry(db, batch.id, f.id, "INQ-HU0619-1", ["ONT8"])
    payload = {"data": [{
        "cmd": 15000, "msgData": {"content": "ONT8 18/kg"},
        "fromRoomId": "100", "senderId": "ext-1", "userId": "me",
        "msgUniqueIdentifier": "wh-1", "timestamp": 1700000000}], "code": 0}
    res = svc.record_incoming(db, payload)
    assert res["stored"] is True
    assert res["attribution"]
    assert res["attribution"][0]["inquiry_id"] == inq.id
