"""卡片按钮动作分发单测 —— 走询价线 stub（接缝），覆盖红线"选定走人工按钮"。"""

from app.feishu import service
from app.feishu import inquiry_port as port
from app.models import Batch, Inquiry


def _seed_batch(db):
    b = Batch(name="批次A", status="已同步")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def test_start_inquiry_creates_draft_card(db):
    b = _seed_batch(db)
    res = service.handle_action(db, chat_id="c", open_id="o",
                                action="start_inquiry", value={"batch_id": b.id})
    assert res["action"] == "start_inquiry"
    # 落了 Inquiry（stub 用真实表留痕），状态待发送
    inq = db.query(Inquiry).filter(Inquiry.batch_id == b.id).first()
    assert inq is not None and inq.status == "待发送"
    # 卡片是"待确认"，且带 确认群发/取消 按钮（不自动发——红线）
    actions = [a for el in res["card"]["elements"] if el.get("tag") == "action"
               for a in el["actions"]]
    names = {a["value"]["action"] for a in actions}
    assert names == {"send_inquiry", "cancel_inquiry"}
    assert res["inquiry_id"] == inq.id


def test_send_inquiry_marks_sent(db):
    inq = Inquiry(batch_id=1, status="待发送", content="x")
    db.add(inq)
    db.commit()
    db.refresh(inq)
    res = service.handle_action(db, chat_id="c", open_id="o",
                                action="send_inquiry", value={"inquiry_id": inq.id})
    db.refresh(inq)
    assert inq.status == "已发送"
    assert res["action"] == "send_inquiry"


def test_cancel_inquiry(db):
    inq = Inquiry(batch_id=1, status="待发送", content="x")
    db.add(inq)
    db.commit()
    db.refresh(inq)
    service.handle_action(db, chat_id="c", open_id="o",
                          action="cancel_inquiry", value={"inquiry_id": inq.id})
    db.refresh(inq)
    assert inq.status == "已取消"


def test_view_comparison_card(db):
    inq = Inquiry(batch_id=1, status="已发送", content="x")
    db.add(inq)
    db.commit()
    db.refresh(inq)
    res = service.handle_action(db, chat_id="c", open_id="o",
                                action="view_comparison", value={"inquiry_id": inq.id})
    assert res["action"] == "view_comparison"
    # stub 给两家报价 → 两个选定按钮
    actions = [a for el in res["card"]["elements"] if el.get("tag") == "action"
               for a in el["actions"]]
    assert {a["value"]["action"] for a in actions} == {"choose_forwarder"}
    assert len(actions) == 2


def test_choose_forwarder_records_choice(db):
    inq = Inquiry(batch_id=1, status="已发送", content="x")
    db.add(inq)
    db.commit()
    db.refresh(inq)
    res = service.handle_action(db, chat_id="c", open_id="o", action="choose_forwarder",
                                value={"inquiry_id": inq.id, "quote_id": 9001})
    db.refresh(inq)
    assert inq.status == "已选货代" and inq.chosen_quote_id == 9001
    assert res["card"]["header"]["title"]["content"] == "已选定货代"


def test_unknown_action(db):
    res = service.handle_action(db, chat_id="c", open_id="o", action="explode", value={})
    assert res["card"]["header"]["title"]["content"] == "未知操作"


def test_missing_params_are_handled(db):
    # 缺 batch_id / inquiry_id 不应抛异常
    r1 = service.handle_action(db, chat_id="c", open_id="o", action="start_inquiry", value={})
    r2 = service.handle_action(db, chat_id="c", open_id="o", action="send_inquiry", value={})
    r3 = service.handle_action(db, chat_id="c", open_id="o", action="choose_forwarder",
                               value={"inquiry_id": 1})
    for r in (r1, r2, r3):
        assert "card" in r


def test_inquiry_port_uses_stub_in_tests():
    # conftest 设了 FEISHU_INQUIRY_STUB=1
    assert port.using_stub() is True
    comp = port.get_comparison(None, 1)
    assert comp["stub"] is True and len(comp["quotes"]) == 2
