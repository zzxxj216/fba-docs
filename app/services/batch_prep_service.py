"""批次「数据准备」聚合 —— SOP 第一步。

把批次按 SKU 汇总成两张清单 + 一份校验：
- 补仓清单：每个 SKU 跨货件的总数量（采购/补仓要的）
- 建仓清单：每个 SKU 的数量 + 箱规（每箱数/长宽高IN/重LB，从产品库补；建仓要的）
- 校验：缺产品库记录 / 缺箱规 / 缺中文报关名 等，逐项列出
ready=True（无校验问题）时，SOP 的「数据准备」步自动判定完成。
"""

from ..models import Product

CM_PER_IN = 2.54
LB_PER_KG = 2.20462


def aggregate(db, batch):
    """按 SKU 聚合 → {replenish, build, issues, ready, total_qty, sku_count}。"""
    agg = {}
    for sp in batch.shipments:
        for it in sp.items:
            msku = (it.msku or "").strip()
            if not msku:
                continue
            a = agg.setdefault(msku, {"qty": 0, "name": ""})
            a["qty"] += it.qty or 0
            if not a["name"] and it.product and it.product.name_customs_cn:
                a["name"] = it.product.name_customs_cn

    replenish, build, issues = [], [], []
    for msku in sorted(agg):
        a = agg[msku]
        p = db.query(Product).filter(Product.sku == msku).first()
        name = a["name"] or (p.name_customs_cn if p else "") or ""
        upb = p.qty_per_box if p and p.qty_per_box else None
        l = round(p.carton_l_cm / CM_PER_IN, 2) if p and p.carton_l_cm else None
        w = round(p.carton_w_cm / CM_PER_IN, 2) if p and p.carton_w_cm else None
        h = round(p.carton_h_cm / CM_PER_IN, 2) if p and p.carton_h_cm else None
        wt = round(p.box_weight_kg * LB_PER_KG, 2) if p and p.box_weight_kg else None
        miss = [k for k, v in (("每箱数", upb), ("箱长", l), ("箱宽", w), ("箱高", h), ("箱重", wt)) if not v]

        replenish.append({"msku": msku, "qty": a["qty"], "product_name": name})
        build.append({"msku": msku, "qty": a["qty"], "units_per_box": upb,
                      "l_in": l, "w_in": w, "h_in": h, "weight_lb": wt, "missing": miss})
        if not p:
            issues.append(f"{msku}：产品库无此 SKU（无法补箱规）")
        elif miss:
            issues.append(f"{msku}：缺 {('、').join(miss)}")
        if not name:
            issues.append(f"{msku}：缺中文报关名")

    return {"replenish": replenish, "build": build, "issues": issues,
            "ready": len(issues) == 0,
            "total_qty": sum(a["qty"] for a in agg.values()), "sku_count": len(agg)}
