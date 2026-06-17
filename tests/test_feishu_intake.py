"""intake 组装 + 认人 + 管辖过滤 单测（SQLite，mock 数据源）。"""

import json

import pytest

from app import llm_client
from app.feishu import intake, service
from app.feishu_models import FeishuSession, Operator
from app.models import Batch, Brand, Product, Shipment, ShipmentItem


def _seed_brand(db, name="Serenorch"):
    b = Brand(name=name, abbr2="SE")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _seed_batch(db, brand, *, shop="Serenorch-US", status="已同步", name="Serenorch-US-0619",
                ready=True):
    batch = Batch(name=name, brand_id=brand.id, shop_name=shop, status=status)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    sp = Shipment(batch_id=batch.id, fc_code="ONT8")
    db.add(sp)
    db.commit()
    db.refresh(sp)
    if ready:
        # 建一个完整产品，让体检 ready=True（SKU 按批次名唯一，避免多批次冲突）
        sku = f"SKU-OK-{batch.id}"
        p = Product(sku=sku, name_customs_cn="台灯", hs_code="9405",
                    usage="照明", material="塑料", brand_name="Serenorch",
                    qty_per_box=10, carton_l_cm=30, carton_w_cm=20, carton_h_cm=10,
                    box_weight_kg=2.0, unit_price_default=5.0)
        db.add(p)
        db.commit()
        item = ShipmentItem(shipment_id=sp.id, msku=sku, qty=100, box_count=10,
                            customs_unit_price=5.0, product_id=p.id)
    else:
        # 缺产品库 → 体检 not ready
        item = ShipmentItem(shipment_id=sp.id, msku=f"SKU-MISSING-{batch.id}",
                            qty=50, box_count=5)
    db.add(item)
    db.commit()
    return batch


def _seed_operator(db, *, open_id="ou_zhang", brand_ids=None, admin=False, name="张三"):
    op = Operator(name=name, feishu_open_id=open_id, is_admin=admin,
                  scope_brand_ids=json.dumps(brand_ids) if brand_ids is not None else None)
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def test_identify_operator(db):
    op = _seed_operator(db, open_id="ou_a")
    assert intake.identify_operator(db, open_id="ou_a").id == op.id
    assert intake.identify_operator(db, open_id="ou_nope") is None


def test_scope_filter_by_brand(db):
    b1 = _seed_brand(db, "Serenorch")
    b2 = _seed_brand(db, "Byane")
    _seed_batch(db, b1, name="SE-batch")
    _seed_batch(db, b2, name="BY-batch", shop="Byane-US")
    op = _seed_operator(db, brand_ids=[b1.id])     # 只管 b1
    batches = intake.list_operator_batches(db, op)
    names = {x.name for x in batches}
    assert names == {"SE-batch"}


def test_admin_sees_all(db):
    b1 = _seed_brand(db, "Serenorch")
    b2 = _seed_brand(db, "Byane")
    _seed_batch(db, b1, name="SE-batch")
    _seed_batch(db, b2, name="BY-batch", shop="Byane-US")
    op = _seed_operator(db, admin=True)
    assert len(intake.list_operator_batches(db, op)) == 2


def test_health_check_ready_and_not(db):
    brand = _seed_brand(db)
    ready_batch = _seed_batch(db, brand, name="ready", ready=True)
    not_ready = _seed_batch(db, brand, name="notready", ready=False)
    hc1 = intake.health_check(db, ready_batch)
    hc2 = intake.health_check(db, not_ready)
    assert hc1["ready"] is True and hc1["issue_count"] == 0
    assert hc2["ready"] is False and hc2["issue_count"] >= 1


def test_build_intake_creates_session_and_todos(db, tmp_path, monkeypatch):
    # 工作文件夹建到临时目录，避免写真实 output/
    monkeypatch.setattr(intake, "_work_dir", lambda sess: str(tmp_path / f"s{sess.id}"))
    brand = _seed_brand(db)
    _seed_batch(db, brand, name="batch1", ready=False)
    op = _seed_operator(db, brand_ids=[brand.id])
    data = intake.build_intake(db, op, chat_id="chat1", open_id="ou_zhang")
    assert isinstance(data["session"], FeishuSession)
    assert data["session"].work_dir.endswith(f"s{data['session'].id}")
    assert len(data["todos"]) == 1
    # 复用会话：同 chat 再 intake 不新建
    sess_id = data["session"].id
    data2 = intake.build_intake(db, op, chat_id="chat1", open_id="ou_zhang")
    assert data2["session"].id == sess_id


def test_handle_message_unknown_operator(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    res = service.handle_message(db, chat_id="c1", open_id="ou_unknown", text="待办")
    assert res["operator"] is None
    assert res["card"]["header"]["title"]["content"] == "未登记"


def test_handle_message_known_operator_returns_todo_card(db, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    monkeypatch.setattr(intake, "_work_dir", lambda sess: str(tmp_path / f"s{sess.id}"))
    brand = _seed_brand(db)
    _seed_batch(db, brand, name="batch1", ready=True)
    op = _seed_operator(db, open_id="ou_zhang", brand_ids=[brand.id])
    res = service.handle_message(db, chat_id="c1", open_id="ou_zhang", text="看待办")
    assert res["operator"] == op.id
    assert len(res["todos"]) == 1
    assert res["card"]["header"]["title"]["content"].startswith("建仓就绪体检")


def test_handle_message_help(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    _seed_operator(db, open_id="ou_zhang")
    res = service.handle_message(db, chat_id="c1", open_id="ou_zhang", text="你能做什么 help")
    assert res["card"]["header"]["title"]["content"] == "我能帮你"
