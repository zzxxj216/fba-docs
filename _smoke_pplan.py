"""采购计划服务冒烟测试（直连函数，不经 HTTP，避免重启 8000 端口服务）。

步骤：
 1. list 已审批采购计划，断言含 PPG2606060001，且 items 有图/店铺/计划量
 2. candidates(PPG2606060001) 返回候选；理想 auto 匹配到 5 货件那票（总量1008）
 3. preview 组装 restock + shipment_plan + 一致性（purchase_total=1008）
 4. import 真实导入（写库）：断言 batch.purchase_plan_no 已设、ShipmentItem.purchase_cost 已填

用法：
  python _smoke_pplan.py            # 步骤 1-3（只读）
  python _smoke_pplan.py --import   # 加上步骤 4（真实写库 PPG2606060001）
"""
import sys

from app.database import SessionLocal
from app.models import Batch, ShipmentItem
from app.services import purchase_plan_service as pps

PGN = "PPG2606060001"
EXPECT_STA = "wfa98afd04-5c98-4de6-92c9-38e46615bb3d"
EXPECT_TOTAL = 1008


def main():
    do_import = "--import" in sys.argv
    db = SessionLocal()
    try:
        # ---- 1. list ----
        print("=== 1. list_plans(page=1) ===")
        res = pps.list_plans(db, page=1)
        plans = res["plans"]
        print(f"plans={len(plans)} page={res['page']} total_size={res['total_size']}")
        target = next((p for p in plans if p["plan_group_no"] == PGN), None)
        assert target, f"page1 未含 {PGN}"
        print(f"  {PGN}: create_name={target['create_name']} create_time={target['create_time']} "
              f"total={target['plan_total_num']} item_count={target['item_count']} "
              f"imported={target['imported']} batch_id={target['batch_id']}")
        it0 = target["items"][0]
        print(f"  item[0]: msku={it0['msku']} name={it0['commodity_name']!r} "
              f"shop={it0['shop_name']} site={it0['site_name']} plan_num={it0['plan_num']} "
              f"carton_num={it0['carton_num']} cost={it0['purchase_cost']} "
              f"image={'Y' if it0['main_image'] else 'N'}")
        assert it0["main_image"], "明细缺图"
        assert it0["shop_name"], "明细缺店铺"
        assert it0["plan_num"], "明细缺计划量"
        print("  [OK] list 断言通过")

        # ---- 2. candidates ----
        print(f"\n=== 2. candidates({PGN}) ===")
        cand = pps.candidates(db, PGN)
        print(f"auto_inbound_plan_id={cand['auto_inbound_plan_id']}")
        for c in cand["candidates"]:
            print(f"  {c['inbound_plan_id']} score={c['match_score']} reason={c['match_reason']} "
                  f"shipments={c['shipment_count']} qty={c['total_qty']} fc={c['fc_list']} "
                  f"imported={c['imported']} batch_id={c['batch_id']}")
        assert cand["candidates"], "candidates 为空"
        auto = cand["auto_inbound_plan_id"]
        if auto == EXPECT_STA:
            print(f"  [OK] auto 匹配到预期 STA {EXPECT_STA}（唯一100分）")
        else:
            hit = next((c for c in cand["candidates"]
                        if c["inbound_plan_id"] == EXPECT_STA), None)
            print(f"  [WARN] auto={auto} != 预期 {EXPECT_STA}；"
                  f"该 STA 在候选中: {bool(hit)}"
                  + (f" score={hit['match_score']} qty={hit['total_qty']}" if hit else ""))

        # ---- 3. preview ----
        chosen = auto or EXPECT_STA
        print(f"\n=== 3. preview({PGN}, {chosen}) ===")
        pv = pps.preview(db, PGN, chosen)
        rs = pv["restock"]
        spn = pv["shipment_plan"]
        cons = pv["consistency"]
        print(f"restock.total_num={rs['total_num']} items={len(rs['items'])}")
        print(f"shipment_plan: shipments={spn['totals']['shipment_count']} "
              f"carton_num={spn['totals']['carton_num']} qty={spn['totals']['qty']}")
        for s in spn["shipments"]:
            print(f"  {s['amazon_shipment_id']} fc={s['fc_code']} ref={s['reference_id']} "
                  f"carton={s['carton_num']} qty={s['total_qty']} addr={s['address'][:40]!r}")
        print(f"consistency: purchase_total={cons['purchase_total']} "
              f"shipment_total={cons['shipment_total']} matched={cons['matched']} "
              f"sku_diff={cons['sku_diff']}")
        assert cons["purchase_total"] == EXPECT_TOTAL, \
            f"purchase_total {cons['purchase_total']} != {EXPECT_TOTAL}"
        print("  [OK] preview 一致性 purchase_total=1008 断言通过")

        # ---- 4. import (写库) ----
        if not do_import:
            print("\n=== 4. import 跳过（加 --import 执行真实写库）===")
            return
        print(f"\n=== 4. import({PGN}, {chosen}) [写库] ===")
        result = pps.import_plan(db, PGN, chosen)
        print(f"batch_id={result['batch_id']} created={result.get('created')} "
              f"purchase_plan_no={result.get('purchase_plan_no')} "
              f"purchase_cost_filled={result.get('purchase_cost_filled')}")
        rep = result["report"]
        errs = [e for e in rep["errors"] if e["level"] == "error"]
        print(f"report: passed={rep['passed']} errors={len(errs)} warns={len(rep['errors'])-len(errs)}")
        for e in rep["errors"][:8]:
            print(f"    [{e['level']}] {e['scope']} {e['field']}: {e['msg']}")

        db.expire_all()
        batch = db.get(Batch, result["batch_id"])
        assert batch.purchase_plan_no == PGN, \
            f"batch.purchase_plan_no={batch.purchase_plan_no} != {PGN}"
        print(f"  [OK] batch.purchase_plan_no={batch.purchase_plan_no}")
        costs = (db.query(ShipmentItem.msku, ShipmentItem.purchase_cost)
                 .join(ShipmentItem.shipment)
                 .filter_by(batch_id=batch.id).all())
        print(f"  ShipmentItem 采购成本: {[(m, c) for m, c in costs]}")
        assert any(c for _, c in costs), "无任何 ShipmentItem.purchase_cost 被填"
        print("  [OK] import 断言通过")
    finally:
        db.close()


if __name__ == "__main__":
    main()
