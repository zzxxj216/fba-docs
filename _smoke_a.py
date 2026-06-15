# -*- coding: utf-8 -*-
"""Agent A 冒烟：真实赛狐 API + 真实 MySQL（会写库）。

跑法: cd D:\\amazon && python _smoke_a.py
顺序: 建表 → list_plans → 品牌导入 → 产品导入 → import_batch(真实计划) → 校验报告
（先导主数据再导批次，品牌/产品才能匹配上）
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.database import Base, SessionLocal, engine
import app.models as models  # noqa: F401  注册全部模型

Base.metadata.create_all(engine)
print("=== 0. 建表 OK ===")

from app.services import import_service, sync_service  # noqa: E402

db = SessionLocal()

print("\n=== 1. 赛狐 list_plans 第1页 ===")
plans = sync_service.list_plans(1) or {}
rows = plans.get("rows") or plans.get("list") or []
print("total:", plans.get("total"), "| 本页:", len(rows))
for r in rows[:5]:
    print("  -", r.get("inboundPlanId"), "|", r.get("inboundPlanName"),
          "| shopId:", r.get("shopId"), "| status:", r.get("status"))
if rows:
    print("  首行字段:", sorted(rows[0].keys()))

print("\n=== 2. 品牌/工厂/主体导入（店铺主体-工厂信息库.xlsx）===")
print(import_service.import_brands(db, r"F:\聊天记录\店铺主体-工厂信息库.xlsx"))

print("\n=== 3. 产品导入（商品列表-报关xlsx.xlsx）===")
print(import_service.import_products(
    db, r"C:\Users\zane\Downloads\Serenorch-US-6.5\商品列表-报关xlsx.xlsx"))

print("\n=== 4. import_batch（真实计划 wfd9f7fa9d-…f1177）===")
res = sync_service.import_batch(db, "wfd9f7fa9d-8ad3-481e-a495-d30fb83c1177")
print("batch_id:", res["batch_id"], "| created:", res["created"])

b = db.get(models.Batch, res["batch_id"])
print("批次:", b.name, "| shop:", b.shop_name, "| mkt:", b.marketplace,
      "| base_date:", b.base_date, "| contract_date:", b.contract_date,
      "| brand_id:", b.brand_id, "| company_id:", b.company_id,
      "| factory_id:", b.factory_id)
for sp in b.shipments:
    print(f"  货件 {sp.amazon_shipment_id} fc={sp.fc_code} 箱={sp.carton_num} "
          f"重={sp.total_weight} 体积={sp.total_volume} items={len(sp.items)} "
          f"boxes={len(sp.boxes)} fw={sp.forwarder_id} sn={sp.ship_sn} "
          f"addr={sp.address_line1},{sp.city},{sp.state} {sp.postal_code}")
    for it in sp.items:
        print(f"    {it.msku} qty={it.qty} box={it.box_count} per={it.qty_per_box} "
              f"箱规={it.carton_l}x{it.carton_w}x{it.carton_h} "
              f"price={it.customs_unit_price} cost={it.purchase_cost} pid={it.product_id}")

print("\n=== 5. 校验报告 ===")
report = res["report"]
print("passed:", report["passed"], "| errors:", len(report["errors"]))
for e in report["errors"]:
    print(f"  [{e['level']}] {e['scope']} {e['field']}: {e['msg']}  -> {e['fix_hint']}")

db.close()
print("\n=== 冒烟完成 ===")
