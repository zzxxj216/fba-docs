"""建仓（亚马逊 FBA STA 入库计划）编排服务。

通过 amazon_fba_client HTTP 调 mcapi 执行 SP-API 各步（异步 operationId 轮询），
本地用 InboundPlan 记录过程与状态。输入来自补仓计划/手动（SKU+数量），
每箱数/箱规从产品库 Product 补全（cm→IN、kg→LB）。SOP 对应：上传建仓模板→分仓→自送海运。

状态机：待建仓→计划已创建→装箱已确认→已提交箱规→分仓方案已生成
        →[人工选目的仓方案]→分仓已确认→（运输/完成在阶段2）
"""

import json
import math

from .. import amazon_fba_client as fba
from ..models import Brand, InboundPlan, Product

CM_PER_IN = 2.54
LB_PER_KG = 2.20462

# 默认发货地址（mcapi FbaSourceAddress 用 snake_case；SOP 发货地，先固定，后续可按公司主体配置）
SOURCE_ADDRESS = {
    "name": "Xiao Wu",
    "company_name": "hangzhou muyufang kejiyouxiangongsi",
    "address_line1": "xinwankejichuangxinyuan 1-417shi",
    "city": "Hangzhou",
    "state_or_province_code": "Zhejiang",
    "country_code": "CN",
    "postal_code": "310000",
    "phone_number": "17767158681",
}

ST_CREATED = "计划已创建"
ST_PACKED = "装箱已确认"
ST_BOXED = "已提交箱规"
ST_PLACEMENT = "分仓方案已生成"
ST_PLACED = "分仓已确认"
ST_FAIL = "失败"


# ---------------------------------------------------------------- helpers

def _get(db, rec_id):
    rec = db.query(InboundPlan).filter(InboundPlan.id == rec_id).first()
    if rec is None:
        raise RuntimeError(f"建仓记录 {rec_id} 不存在")
    return rec


def _store(db, brand_id):
    if not brand_id:
        return ""
    b = db.query(Brand).filter(Brand.id == brand_id).first()
    return (b.amazon_store or "") if b else ""


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except (ValueError, TypeError):
        return default


def _dict(rec):
    return {
        "id": rec.id,
        "source_type": rec.source_type,
        "source_ref": rec.source_ref,
        "brand_id": rec.brand_id,
        "store": rec.store,
        "name": rec.name,
        "status": rec.status,
        "amazon_inbound_plan_id": rec.amazon_inbound_plan_id,
        "items": _loads(rec.items_snapshot, []),
        "placement_option_id": rec.placement_option_id,
        "shipments": _loads(rec.shipments_snapshot, {}),
        "current_operation_id": rec.current_operation_id,
        "batch_id": rec.batch_id,
        "error": rec.error or "",
        "created_at": rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
    }


def _resolve_items(db, raw_items):
    """补全/校验建仓明细。

    raw_items: [{msku, quantity, units_per_box?, l_in?, w_in?, h_in?, weight_lb?, expiration?}]
    缺的从 Product 补（qty_per_box / carton_cm→IN / box_weight_kg→LB）；缺关键项报错。
    """
    out = []
    for r in raw_items or []:
        msku = (r.get("msku") or "").strip()
        qty = int(r.get("quantity") or 0)
        if not msku or qty <= 0:
            raise RuntimeError(f"明细缺 msku 或数量<=0：{r}")
        p = db.query(Product).filter(Product.sku == msku).first()
        upb = int(r.get("units_per_box") or (p.qty_per_box if p and p.qty_per_box else 0) or 0)
        l_in = r.get("l_in") or (round(p.carton_l_cm / CM_PER_IN, 2) if p and p.carton_l_cm else None)
        w_in = r.get("w_in") or (round(p.carton_w_cm / CM_PER_IN, 2) if p and p.carton_w_cm else None)
        h_in = r.get("h_in") or (round(p.carton_h_cm / CM_PER_IN, 2) if p and p.carton_h_cm else None)
        wt = r.get("weight_lb") or (round(p.box_weight_kg * LB_PER_KG, 2) if p and p.box_weight_kg else None)
        miss = [k for k, v in (("每箱数", upb), ("箱长", l_in), ("箱宽", w_in), ("箱高", h_in), ("箱重", wt)) if not v]
        if miss:
            raise RuntimeError(f"SKU {msku} 缺 {('、').join(miss)}（请在产品库补箱规，或建仓时手填）")
        out.append({"msku": msku, "quantity": qty, "units_per_box": upb,
                    "l_in": l_in, "w_in": w_in, "h_in": h_in, "weight_lb": wt,
                    "expiration": (r.get("expiration") or "").strip()})
    if not out:
        raise RuntimeError("建仓明细为空")
    return out


# ---------------------------------------------------------------- 建仓步骤

def create_plan(db, brand_id, raw_items, source_type="manual", source_ref="", name=""):
    """① 创建入库计划（SKU+数量+发货地址）。"""
    items = _resolve_items(db, raw_items)
    store = _store(db, brand_id)
    api_items = []
    for it in items:
        d = {"msku": it["msku"], "quantity": it["quantity"], "prep_owner": "SELLER", "label_owner": "SELLER"}
        if it["expiration"]:
            d["expiration"] = it["expiration"]
        api_items.append(d)
    resp = fba.call("POST", "/inbound-plans", store=store, timeout=90,
                    json={"name": name or None, "source_address": SOURCE_ADDRESS, "items": api_items})
    plan_id = resp.get("inboundPlanId", "")
    op = resp.get("operationId", "")
    rec = InboundPlan(
        source_type=source_type, source_ref=source_ref, brand_id=brand_id, store=store or "",
        name=name, status=ST_CREATED, amazon_inbound_plan_id=plan_id, current_operation_id=op,
        items_snapshot=json.dumps(items, ensure_ascii=False),
        source_address=json.dumps(SOURCE_ADDRESS, ensure_ascii=False))
    db.add(rec)
    db.commit()
    db.refresh(rec)
    if op:
        fba.wait_operation(op, store=store or None, timeout=120)
    return _dict(rec)


def gen_confirm_packing(db, rec_id):
    """② 生成并确认装箱方案（取第一个方案，MVP 单装箱组）。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    pid = rec.amazon_inbound_plan_id
    g = fba.call("POST", f"/inbound-plans/{pid}/packing-options/generate", store=store)
    fba.wait_operation(g["operationId"], store=store)
    opts = fba.call("GET", f"/inbound-plans/{pid}/packing-options", store=store) or {}
    plist = opts.get("packingOptions") or []
    if not plist:
        raise RuntimeError("没有可用装箱方案")
    po = plist[0]
    po_id = po["packingOptionId"]
    groups = po.get("packingGroups") or []
    c = fba.call("POST", f"/inbound-plans/{pid}/packing-options/{po_id}/confirm", store=store)
    fba.wait_operation(c["operationId"], store=store)
    rec.packing_option_id = po_id
    rec.shipments_snapshot = json.dumps({"packing_groups": groups}, ensure_ascii=False)
    rec.status = ST_PACKED
    db.commit()
    return _dict(rec)


def submit_boxes(db, rec_id):
    """③ 提交箱规：按每箱数把每个 SKU 分箱（满箱+余箱），透传 SP-API packageGroupings。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    pid = rec.amazon_inbound_plan_id
    items = _loads(rec.items_snapshot, [])
    groups = _loads(rec.shipments_snapshot, {}).get("packing_groups") or []
    if not groups:
        raise RuntimeError("缺装箱组，请先确认装箱方案")
    group_id = groups[0]   # MVP：单装箱组
    boxes = []
    for it in items:
        upb = it["units_per_box"]
        full, rem = divmod(it["quantity"], upb)
        dims = {"unitOfMeasurement": "IN", "length": it["l_in"], "width": it["w_in"], "height": it["h_in"]}
        wt = {"unit": "LB", "value": it["weight_lb"]}
        if full > 0:
            boxes.append({"contentInformationSource": "BOX_CONTENT_PROVIDED",
                          "items": [{"msku": it["msku"], "quantity": upb, "prepOwner": "SELLER", "labelOwner": "SELLER"}],
                          "dimensions": dims, "weight": wt, "quantity": full})
        if rem > 0:
            boxes.append({"contentInformationSource": "BOX_CONTENT_PROVIDED",
                          "items": [{"msku": it["msku"], "quantity": rem, "prepOwner": "SELLER", "labelOwner": "SELLER"}],
                          "dimensions": dims, "weight": wt, "quantity": 1})
    pg = [{"packingGroupId": group_id, "boxes": boxes}]
    r = fba.call("POST", f"/inbound-plans/{pid}/packing-information", store=store,
                 json={"package_groupings": pg})
    fba.wait_operation(r["operationId"], store=store)
    rec.status = ST_BOXED
    db.commit()
    return _dict(rec)


def gen_placement(db, rec_id):
    """④ 生成分仓方案（耗时较长，最长 300s）。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    pid = rec.amazon_inbound_plan_id
    g = fba.call("POST", f"/inbound-plans/{pid}/placement-options/generate", store=store)
    rec.current_operation_id = g["operationId"]
    db.commit()
    fba.wait_operation(g["operationId"], store=store, timeout=300)
    rec.status = ST_PLACEMENT
    rec.current_operation_id = ""
    db.commit()
    return _dict(rec)


def list_placements(db, rec_id):
    """列分仓方案（各方案的目的仓 FC + 费用），供前端人工选。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    pid = rec.amazon_inbound_plan_id
    opts = fba.call("GET", f"/inbound-plans/{pid}/placement-options", store=store) or {}
    out = []
    for o in opts.get("placementOptions") or []:
        fees = 0.0
        for f in o.get("fees") or []:
            try:
                fees += float(f.get("value", {}).get("amount") or 0)
            except (TypeError, ValueError):
                pass
        ships = []
        for sid in o.get("shipmentIds") or []:
            sh = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sid}", store=store) or {}
            dest = sh.get("destination", {}) or {}
            addr = dest.get("address", {}) or {}
            ships.append({"shipmentId": sid, "fc": dest.get("warehouseId"),
                          "city": addr.get("city"), "state": addr.get("stateOrProvinceCode")})
        out.append({"placement_option_id": o.get("placementOptionId"), "status": o.get("status"),
                    "fees": round(fees, 2), "shipment_count": len(o.get("shipmentIds") or []),
                    "shipments": ships})
    return {"placement_options": out}


def choose_placement(db, rec_id, placement_option_id):
    """⑤ 确认分仓方案（正式生成货件），落地各货件 FC/确认号。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    pid = rec.amazon_inbound_plan_id
    c = fba.call("POST", f"/inbound-plans/{pid}/placement-options/{placement_option_id}/confirm", store=store)
    fba.wait_operation(c["operationId"], store=store, timeout=300)
    opts = fba.call("GET", f"/inbound-plans/{pid}/placement-options", store=store) or {}
    chosen = next((o for o in opts.get("placementOptions") or []
                   if o.get("placementOptionId") == placement_option_id), None)
    ships = []
    for sid in (chosen.get("shipmentIds") if chosen else []) or []:
        sh = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sid}", store=store) or {}
        dest = sh.get("destination", {}) or {}
        addr = dest.get("address", {}) or {}
        ships.append({"shipmentId": sid, "fc": dest.get("warehouseId"),
                      "confirmationId": sh.get("shipmentConfirmationId"),
                      "city": addr.get("city"), "state": addr.get("stateOrProvinceCode"),
                      "postalCode": addr.get("postalCode"), "addressLine1": addr.get("addressLine1"),
                      "status": sh.get("status")})
    rec.placement_option_id = placement_option_id
    rec.shipments_snapshot = json.dumps({"shipments": ships}, ensure_ascii=False)
    rec.status = ST_PLACED
    db.commit()
    return _dict(rec)


# ---------------------------------------------------------------- 列表 / 取消 / 操作轮询

def list_plans(db):
    rows = db.query(InboundPlan).order_by(InboundPlan.id.desc()).all()
    return {"plans": [_dict(r) for r in rows]}


def get_plan(db, rec_id):
    return _dict(_get(db, rec_id))


def cancel_plan(db, rec_id):
    """取消建仓：调 mcapi 取消亚马逊入库计划 + 本地标记。"""
    rec = _get(db, rec_id)
    store = rec.store or None
    if rec.amazon_inbound_plan_id:
        try:
            c = fba.call("PUT", f"/inbound-plans/{rec.amazon_inbound_plan_id}/cancel", store=store)
            if c.get("operationId"):
                fba.wait_operation(c["operationId"], store=store, timeout=120)
        except RuntimeError as e:
            rec.error = f"取消失败：{e}"
            db.commit()
            raise
    rec.status = "已取消"
    db.commit()
    return _dict(rec)


def operation_status(rec_id, operation_id, store=None):
    """前端轮询：转发 mcapi 操作状态（不阻塞等待）。"""
    op = fba.call("GET", f"/operations/{operation_id}", store=store or None) or {}
    return {"operation_id": operation_id, "status": op.get("operationStatus"),
            "problems": op.get("operationProblems")}
