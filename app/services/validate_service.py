"""批次校验：generate 前强制调用。报告格式见 CONTRACTS.md。

{"passed": bool, "errors": [{"level":"error|warn", "scope":"batch|shipment:3|item:9",
  "field":"hs_code", "msg":"...", "fix_hint":"products:12"}]}

errors 里出现 level=error 即 passed=False。
fix_hint 供前端跳转："products:{id}" / "shipments:{id}" / "companies:{id}"，
未匹配产品库时用 "products:0"（0=去产品库新建）。

自动建档分级（sync_service 同步时对未匹配 MSKU 自动建档，remark 含"自动建档"）：
- product_id 非空 → 不再报"未匹配产品库"error；补一条 warn 提示人工核对
- 自动建档产品缺报关必填字段 → 仍是 error（生成报关文件会错），文案改为"自动建档产品缺XX"
- product_id 仍为空（建档失败）→ 保持 error
"""

from ..models import Batch

# 产品必填：字段 → (中文名, level)
PRODUCT_REQUIRED = [
    ("name_customs_cn", "中文报关名", "error"),
    ("name_customs_en", "英文报关名", "error"),
    ("hs_code", "HS编码", "error"),
    ("material", "材质", "error"),
    ("declare_elements", "申报要素", "warn"),
]


def validate_batch(db, batch_id):
    batch = db.get(Batch, batch_id)
    errors = []

    def add(level, scope, field, msg, fix_hint=""):
        errors.append({"level": level, "scope": scope, "field": field,
                       "msg": msg, "fix_hint": fix_hint})

    if batch is None:
        add("error", "batch", "id", f"批次 {batch_id} 不存在")
        return {"passed": False, "errors": errors}

    # ---- 批次绑定 ----
    hint = f"batches:{batch.id}"
    if not batch.brand_id:
        add("error", "batch", "brand_id",
            f"批次未绑定品牌（店铺 {batch.shop_name or '?'} 未匹配到品牌库）", hint)
    if not batch.company_id:
        add("error", "batch", "company_id", "批次未绑定店铺主体", hint)
    if not batch.factory_id:
        add("error", "batch", "factory_id", "批次未绑定供货工厂", hint)

    # ---- 主体英文抬头（报关需要） ----
    company = batch.company
    if company:
        if not (company.name_en or "").strip():
            add("error", "batch", "company.name_en",
                f"主体 {company.name_cn} 缺英文名（报关需英文抬头）",
                f"companies:{company.id}")
        if not (company.address_en or "").strip():
            add("error", "batch", "company.address_en",
                f"主体 {company.name_cn} 缺英文地址（报关需英文抬头）",
                f"companies:{company.id}")

    # ---- 货件 / 明细 ----
    checked_products = set()
    for sp in batch.shipments:
        sscope = f"shipment:{sp.id}"
        slabel = sp.amazon_shipment_id or sp.fc_code or f"#{sp.id}"
        if not sp.items:
            add("error", sscope, "items", f"货件 {slabel} 无明细行",
                f"shipments:{sp.id}")
            continue

        # Σ明细箱数 ↔ 货件箱数
        box_sum = sum(it.box_count or 0 for it in sp.items)
        if sp.carton_num and box_sum != sp.carton_num:
            add("error", sscope, "carton_num",
                f"货件 {slabel} Σ明细箱数 {box_sum} ≠ 货件箱数 {sp.carton_num}",
                f"shipments:{sp.id}")

        for it in sp.items:
            iscope = f"item:{it.id}"
            # 数值必填（None 或 0 都算缺）
            for field, label in (("qty", "数量"), ("box_count", "箱数"),
                                 ("customs_unit_price", "申报单价"),
                                 ("purchase_cost", "采购成本")):
                if not getattr(it, field):
                    add("error", iscope, field,
                        f"货件 {slabel} 明细 {it.msku} 缺{label}",
                        f"shipments:{sp.id}")

            # 产品库匹配 & 必填
            if not it.product_id:
                add("error", iscope, "product_id",
                    f"货件 {slabel} 明细 MSKU {it.msku} 未匹配产品库"
                    "（同步自动建档失败，请手动建档）", "products:0")
                continue
            p = it.product
            if p is None or p.id in checked_products:
                continue
            checked_products.add(p.id)
            phint = f"products:{p.id}"
            is_auto = "自动建档" in (p.remark or "")
            if is_auto:
                add("warn", iscope, "product_id",
                    f"产品 {p.sku} 为同步自动建档，请人工核对/补全报关信息", phint)
            for field, label, level in PRODUCT_REQUIRED:
                if not (getattr(p, field) or "").strip():
                    msg = (f"自动建档产品 {p.sku} 缺{label}，请补全" if is_auto
                           else f"产品 {p.sku} 缺{label}")
                    add(level, iscope, field, msg, phint)
            if not p.box_weight_kg:
                msg = (f"自动建档产品 {p.sku} 缺单箱重量（毛重无法计算），请补全"
                       if is_auto else f"产品 {p.sku} 缺单箱重量（毛重无法计算）")
                add("error", iscope, "box_weight_kg", msg, phint)

    passed = not any(e["level"] == "error" for e in errors)
    return {"passed": passed, "errors": errors}
