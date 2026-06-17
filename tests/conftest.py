"""测试夹具：用 SQLite 内存库（不污染共享 MySQL fba_docs）。

直接拿 models 的 Base 在 SQLite 上 create_all，每个测试一个独立 session（函数级回滚/丢库）。
LLM/企微默认不接通（无网络调用）——测试要么走确定性正则，要么 monkeypatch llm_client。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 让测试无视 .env / MySQL：在导入 app.database 前指向 SQLite，避免连真实库。
os.environ["DB_URL"] = "sqlite+pysqlite:///:memory:"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import models  # noqa: E402  触发所有表注册到 Base
from app.database import Base  # noqa: E402


@pytest.fixture()
def db():
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
