# -*- coding: utf-8 -*-
"""Smoke：MSKU 未匹配 → 同步自动建档 → 校验降级。

批次 1 当前所有 MSKU 都已匹配产品库，因此先把产品 Serenorch-FT-1 的 sku 临时改名
模拟"未匹配"，重跑 import_batch 触发自动建档，再走 /api/batches/1/validate 验证
不再出现"未匹配产品库" error。最后恢复现场（删自动建档产品、还原 sku、回指明细）。

运行：python _smoke_autoproduct.py
"""

import sys

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Batch, Product, ShipmentItem
from app.services import sync_service

TARGET_SKU = "Serenorch-FT-1"
BAK_SKU = TARGET_SKU + "__SMOKEBAK"

OK, FAIL = "[OK]", "[FAIL]"
failures = []


def check(cond, msg):
    print((OK if cond else FAIL), msg)
    if not cond:
        failures.append(msg)


def restore(db, orig_id):
    """删自动建档产品 → 明细回指原产品 → 还原原产品 sku（顺序敏感：先删再还原，
    否则同一 flush 里 UPDATE 先于 DELETE 执行会撞 sku 唯一键）。可重入。"""
    db.rollback()
    auto = (db.query(Product)
            .filter(Product.sku == TARGET_SKU, Product.id != orig_id).first())
    if auto:
        (db.query(ShipmentItem)
           .filter(ShipmentItem.product_id == auto.id)
           .update({ShipmentItem.product_id: orig_id}))
        db.delete(auto)
        db.flush()
    (db.query(Product).filter(Product.id == orig_id)
       .update({Product.sku: TARGET_SKU}))
    db.commit()


def main():
    db = SessionLocal()
    batch = db.get(Batch, 1)
    assert batch, "批次 1 不存在"
    plan_id = batch.inbound_plan_id

    # 原产品：优先按 BAK 名找（上次中断的残留），否则按正式 sku 找非自动建档的那条
    orig = (db.query(Product).filter(Product.sku == BAK_SKU).first()
            or db.query(Product).filter(Product.sku == TARGET_SKU)
                 .filter(~Product.remark.like("%自动建档%") | Product.remark.is_(None))
                 .first())
    assert orig, f"产品 {TARGET_SKU} 不在库（前置条件不满足）"
    orig_id = orig.id
    restore(db, orig_id)                 # 清掉可能的上次残留，确保基线
    orig = db.get(Product, orig_id)

    print(f"== 模拟未匹配：产品#{orig_id} sku {TARGET_SKU} -> {BAK_SKU}")
    orig.sku = BAK_SKU
    db.commit()

    try:
        # ---- 重跑同步：应触发自动建档 ----
        print(f"== import_batch({plan_id}) ...")
        result = sync_service.import_batch(db, plan_id)
        report = result["report"]

        auto = db.query(Product).filter(Product.sku == TARGET_SKU).first()
        check(auto is not None, f"自动建档产品已创建 sku={TARGET_SKU}")
        if auto:
            check("自动建档" in (auto.remark or ""), f"remark 标记: {auto.remark}")
            print(f"     id={auto.id} name_customs_cn={auto.name_customs_cn!r} "
                  f"name_customs_en={auto.name_customs_en!r}")
            print(f"     name_invoice={auto.name_invoice!r} hs_code={auto.hs_code!r} "
                  f"material={auto.material!r} brand={auto.brand_name!r}")
            print(f"     unit_price={auto.unit_price_default} cost={auto.purchase_cost_default} "
                  f"carton={auto.carton_l_cm}x{auto.carton_w_cm}x{auto.carton_h_cm}cm "
                  f"qty/box={auto.qty_per_box} box_kg={auto.box_weight_kg}")
            print(f"     image_url={(auto.image_url or '')[:60]}...")
            check(bool(auto.name_customs_en), "name_customs_en 已带 commodityName")
            check(bool(auto.hs_code), "hs_code 已从商品接口补全")

            items = (db.query(ShipmentItem)
                     .filter(ShipmentItem.msku == TARGET_SKU).all())
            check(items and all(i.product_id == auto.id for i in items),
                  f"{len(items)} 条明细已关联自动建档产品 product_id={auto.id}")

        # ---- TestClient 校验 ----
        client = TestClient(app)
        r = client.get("/api/batches/1/validate")
        check(r.status_code == 200, f"GET /api/batches/1/validate -> {r.status_code}")
        rep = r.json()
        errs = rep["errors"]
        unmatched = [e for e in errs
                     if e["level"] == "error" and "未匹配产品库" in e["msg"]]
        check(not unmatched, f"无『未匹配产品库』error（共 {len(errs)} 条校验信息）")
        auto_warns = [e for e in errs
                      if e["level"] == "warn" and "自动建档" in e["msg"]]
        check(bool(auto_warns), "存在自动建档 warn 提示")
        print("-- 与自动建档产品相关的校验信息：")
        for e in errs:
            if "自动建档" in e["msg"] or TARGET_SKU in e["msg"]:
                print(f"   [{e['level']}] {e['msg']} (fix_hint={e['fix_hint']})")
        # 同步返回的报告应与单独校验一致（也不含未匹配 error）
        check(not any(e["level"] == "error" and "未匹配产品库" in e["msg"]
                      for e in report["errors"]),
              "import_batch 返回的 report 同样无『未匹配产品库』error")
    finally:
        # ---- 恢复现场 ----
        print("== 恢复现场 ...")
        restore(db, orig_id)
        restored = db.query(Product).filter(Product.sku == TARGET_SKU).first()
        items_ok = all(i.product_id == orig_id for i in
                       db.query(ShipmentItem)
                         .filter(ShipmentItem.msku == TARGET_SKU).all())
        print(f"   产品#{restored.id if restored else '?'} sku 已还原，"
              f"自动建档产品已删除，明细已回指={items_ok}")
        db.close()

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} 项断言失败")
        sys.exit(1)
    print("SMOKE PASSED")


if __name__ == "__main__":
    main()
