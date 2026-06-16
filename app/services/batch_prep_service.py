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
    """按 SKU 聚合成"建仓前数据体检" → {detail, replenish, build, issues, ready, total_qty, sku_count}。

    detail：每个 SKU 一行，含 数量/每箱/外箱/箱规(in)/箱重(lb)/申报单价/HS/申报要素/是否自动建档，
            并逐行列出 problems（缺箱规/缺品名/缺申报价/缺HS/缺申报要素/自动建档待核对）。
    建仓前可能有问题的数据全部显示出来，便于先补齐再建仓。
    """
    agg = {}
    for sp in batch.shipments:
        for it in sp.items:
            msku = (it.msku or "").strip()
            if not msku:
                continue
            a = agg.setdefault(msku, {"qty": 0, "boxes": 0, "name": "", "unit_price": None})
            a["qty"] += it.qty or 0
            a["boxes"] += it.box_count or 0
            if a["unit_price"] is None and it.customs_unit_price:
                a["unit_price"] = it.customs_unit_price
            if not a["name"] and it.product and it.product.name_customs_cn:
                a["name"] = it.product.name_customs_cn

    detail, replenish, build, issues = [], [], [], []
    for msku in sorted(agg):
        a = agg[msku]
        p = db.query(Product).filter(Product.sku == msku).first()
        name = a["name"] or (p.name_customs_cn if p else "") or ""
        upb = p.qty_per_box if p and p.qty_per_box else None
        l = round(p.carton_l_cm / CM_PER_IN, 2) if p and p.carton_l_cm else None
        w = round(p.carton_w_cm / CM_PER_IN, 2) if p and p.carton_w_cm else None
        h = round(p.carton_h_cm / CM_PER_IN, 2) if p and p.carton_h_cm else None
        wt = round(p.box_weight_kg * LB_PER_KG, 2) if p and p.box_weight_kg else None
        unit_price = a["unit_price"] or (p.unit_price_default if p else None)
        hs = (p.hs_code if p else "") or ""
        auto = bool(p and "自动建档" in (p.remark or ""))
        box_miss = [k for k, v in (("每箱数", upb), ("长", l), ("宽", w), ("高", h), ("重", wt)) if not v]
        # 报关单的申报要素由 用途/材质/品牌(/型号) 现拼（见 field_registry._declare_full），
        # 故检查这三项而非赛狐常空着的原始 declareElements 字段，避免误报。
        decl_miss = [k for k, v in (("用途", p.usage if p else ""),
                                    ("材质", p.material if p else ""),
                                    ("品牌", p.brand_name if p else "")) if not (v or "").strip()]

        problems = []
        if not p:
            problems.append("产品库无此SKU")
        if not name:
            problems.append("缺品名")
        if box_miss:
            problems.append("缺箱规(" + "、".join(box_miss) + ")")
        if not unit_price:
            problems.append("缺申报单价")
        if not hs:
            problems.append("缺HS编码")
        if p and decl_miss:
            problems.append("缺申报要素(" + "、".join(decl_miss) + ")")
        if auto:
            problems.append("自动建档待核对")

        # 阻断 ready 的硬问题（建仓/报关必需）；自动建档/申报要素只是提醒
        hard = bool((not p) or box_miss or (not name) or (not unit_price) or (not hs))

        detail.append({
            "msku": msku, "product_name": name, "qty": a["qty"], "boxes": a["boxes"],
            "units_per_box": upb, "l_in": l, "w_in": w, "h_in": h, "weight_lb": wt,
            "customs_unit_price": unit_price, "hs_code": hs,
            "has_declare": not decl_miss, "auto_created": auto,
            "in_lib": bool(p), "problems": problems, "ok": not hard,
        })
        replenish.append({"msku": msku, "qty": a["qty"], "product_name": name})
        build.append({"msku": msku, "qty": a["qty"], "units_per_box": upb,
                      "l_in": l, "w_in": w, "h_in": h, "weight_lb": wt, "missing": box_miss})
        if problems:
            issues.append(f"{msku}：{('、').join(problems)}")

    ready = all(d["ok"] for d in detail)
    return {"detail": detail, "replenish": replenish, "build": build, "issues": issues,
            "ready": ready, "total_qty": sum(a["qty"] for a in agg.values()), "sku_count": len(agg)}


def fill_missing_products(db, batch):
    """从赛狐商品接口对齐批次产品资料。返回 {created, failed, refreshed}。

    - 未匹配的 SKU → 自动建档
    - 已存在的 SKU → 回填仍为空的报关/箱规字段（不覆盖已有值）
    没有 STA 的批次（补仓/采购计划导入）走这条路当"重新同步"。
    """
    from . import sync_service as ss
    seen, created, failed, refreshed = set(), [], [], []
    for sp in batch.shipments:
        for it in sp.items:
            msku = (it.msku or "").strip()
            if not msku or msku in seen:
                continue
            seen.add(msku)
            p = db.query(Product).filter(Product.sku == msku).first()
            if p is None:
                raw = {"childItems": [{"commoditySku": msku}], "cartonQty": it.qty_per_box}
                p = ss._auto_create_product(db, msku, raw)
                (created if p else failed).append(msku)
            else:
                try:
                    if ss.refresh_product_declares(db, p):
                        refreshed.append(msku)
                except Exception:
                    pass
    # 回链产品 + 补申报单价到明细（无论是否新建，方便修历史批次）
    for sp in batch.shipments:
        for it in sp.items:
            msku = (it.msku or "").strip()
            if not msku:
                continue
            p = db.query(Product).filter(Product.sku == msku).first()
            if not p:
                continue
            if it.product_id is None:
                it.product_id = p.id
            if (it.customs_unit_price in (None, 0)) and p.unit_price_default:
                it.customs_unit_price = p.unit_price_default
    db.commit()
    return {"created": created, "failed": failed, "refreshed": refreshed}
