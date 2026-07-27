"""测试夹具：用 SQLite 内存库（不污染共享业务库），覆盖询价线等。

- 导入 app.database 前把 DB_URL 指向 SQLite，避免连真实 MySQL。
- LLM/企微默认不接通；要测智能逻辑就 monkeypatch llm_client。
"""

import os
import sys

# 必须在导入 app.database 之前设置，避免 import 时连 MySQL。
os.environ["DB_URL"] = "sqlite+pysqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: E402  触发全部表注册到 Base
from app.database import Base  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    """内存 SQLite 会话；建全部表，每个测试独立、结束丢库。"""
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_batch(db, fcs):
    """建一个带若干目的仓 shipment 的批次。fcs=[(fc, boxes, weight, volume), ...]。"""
    from app.models import Batch, Brand, Shipment, ShipmentItem
    brand = Brand(name="Huhole", abbr2="HU")
    db.add(brand)
    db.flush()
    batch = Batch(inbound_plan_id=f"PLN{len(fcs)}", name="Huhole-US-0619",
                  brand_id=brand.id, country="US")
    db.add(batch)
    db.flush()
    for fc, boxes, weight, volume in fcs:
        sh = Shipment(batch_id=batch.id, fc_code=fc, carton_num=boxes,
                      total_weight=weight, total_volume=volume)
        db.add(sh)
        db.flush()
        db.add(ShipmentItem(shipment_id=sh.id, msku=f"SKU-{fc}", qty=boxes * 10))
    db.commit()
    return batch


def make_forwarder(db, name, brand_id=None, room_id="", default=False):
    from app.models import Forwarder
    f = Forwarder(name=name, bind_brand_id=brand_id, qiwe_room_id=room_id,
                  is_default=default, active=True)
    db.add(f)
    db.commit()
    return f
