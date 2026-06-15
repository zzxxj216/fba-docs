# -*- coding: utf-8 -*-
"""离线历史批次导入冒烟测试（不起服务，直接调 import_service）。

用真实案例 Serenorch-US-6.5 发货单导出文件验证：
- 5 个货件、FC 集合、各货件 reference_id / FBA 号 / 箱数 / 明细 qty
- 幂等：同文件重跑不重复建批次/货件/明细
运行: python _smoke_offline.py   （需要本地 MySQL 就绪）
"""

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

CASE = r"C:\Users\zane\Downloads\Serenorch-US-6.5\发货单-20260529-1780017266088.xlsx"

from app.database import Base, SessionLocal, engine          # noqa: E402
from app.models import Batch, Shipment, ShipmentItem          # noqa: E402
from app.services import import_service                       # noqa: E402

Base.metadata.create_all(bind=engine)

# FBA号 -> (reference_id, FC, 箱数, 明细qty)   —— 取自案例文件两个 sheet 的关联
EXPECT = {
    "FBA19F4J32JK": ("3KMDMW3R", "GYR2", 8, 192),
    "FBA19F4KS9X6": ("5217RHQS", "LGB8", 6, 144),
    "FBA19F4M4MYR": ("5MZ4OA3H", "LAS1", 10, 240),
    "FBA19F4PMVY9": ("36QPZRPH", "ABE8", 14, 336),
    "FBA19F4PN8RQ": ("5VX5K1WB", "IND9", 12, 288),
}
EXPECT_FCS = {"IND9", "ABE8", "LAS1", "LGB8", "GYR2"}


def check(db, batch_id, label):
    batch = db.get(Batch, batch_id)
    assert batch is not None, "批次不存在"
    assert batch.inbound_plan_id.startswith("offline-"), batch.inbound_plan_id
    ships = list(batch.shipments)
    assert len(ships) == 5, f"货件数 {len(ships)} != 5"
    assert {s.fc_code for s in ships} == EXPECT_FCS, {s.fc_code for s in ships}
    for s in ships:
        assert s.amazon_shipment_id in EXPECT, f"未知货件 {s.amazon_shipment_id}"
        ref, fc, boxes, qty = EXPECT[s.amazon_shipment_id]
        assert s.reference_id == ref, f"{s.amazon_shipment_id} ref {s.reference_id} != {ref}"
        assert s.fc_code == fc, f"{s.amazon_shipment_id} fc {s.fc_code} != {fc}"
        assert s.carton_num == boxes, f"{s.amazon_shipment_id} 箱数 {s.carton_num} != {boxes}"
        assert "offline" in (s.remark or ""), f"{s.amazon_shipment_id} 缺 offline 标记"
        items = list(s.items)
        assert len(items) == 1, f"{s.amazon_shipment_id} 明细数 {len(items)} != 1"
        it = items[0]
        assert it.msku == "Serenorch-FT-1", it.msku
        assert it.qty == qty, f"{s.amazon_shipment_id} qty {it.qty} != {qty}"
        # 单 SKU 货件：明细箱数 = 货件箱数；每箱数量 = qty/箱数（整除时）
        assert it.box_count == boxes, f"{s.amazon_shipment_id} box_count {it.box_count} != {boxes}"
        assert it.qty_per_box == qty // boxes, f"{s.amazon_shipment_id} qty_per_box {it.qty_per_box}"
    print(f"[{label}] OK  batch_id={batch.id} name={batch.name} "
          f"plan={batch.inbound_plan_id} shop={batch.shop_name} country={batch.country} "
          f"base_date={batch.base_date} contract_date={batch.contract_date} "
          f"brand_id={batch.brand_id} company_id={batch.company_id} factory_id={batch.factory_id}")


def main():
    db = SessionLocal()
    try:
        r1 = import_service.import_shipping_order(db, CASE)
        check(db, r1["batch_id"], "第一次导入")

        r2 = import_service.import_shipping_order(db, CASE, batch_name="")
        assert r2["batch_id"] == r1["batch_id"], "幂等失败：重跑产生了新批次"
        assert r2["created"] is False, "幂等失败：重跑 created 应为 False"
        db.expire_all()
        check(db, r2["batch_id"], "幂等重跑")

        n_ship = db.query(Shipment).filter(Shipment.batch_id == r1["batch_id"]).count()
        n_item = (db.query(ShipmentItem).join(Shipment)
                  .filter(Shipment.batch_id == r1["batch_id"]).count())
        assert n_ship == 5 and n_item == 5, f"重跑后数量异常 ships={n_ship} items={n_item}"

        report = r2["report"]
        print(f"校验报告 passed={report['passed']}，{len(report['errors'])} 条：")
        for e in report["errors"]:
            print(f"  - [{e['level']}] {e['scope']} {e['field']}: {e['msg']}")
        print("SMOKE PASS")
    finally:
        db.close()


if __name__ == "__main__":
    main()
