"""建仓（亚马逊 FBA STA 入库计划）编排服务。

通过 amazon_fba_client HTTP 调 mcapi 执行 SP-API 各步（异步 operationId 轮询），
本地用 InboundPlan 记录过程与状态。输入来自补仓计划/手动（SKU+数量），
每箱数/箱规从产品库 Product 补全（cm→IN、kg→LB）。SOP 对应：上传建仓模板→分仓→自送海运。

状态机：待建仓→计划已创建→装箱已确认→已提交箱规→分仓方案已生成
        →[人工选目的仓方案]→分仓已确认→（运输/完成在阶段2）
"""

import io
import json
import math
import re

from openpyxl import load_workbook

from .. import amazon_fba_client as fba
from ..models import Brand, InboundPlan, Product

CM_PER_IN = 2.54
LB_PER_KG = 2.20462

# 发货地址（mcapi FbaSourceAddress 用 snake_case）。**按店铺(amazon_store)配置**——
# 不同店铺货物从不同地点发，建仓 sourceAddress 必须用对应店铺自己的发货地。
SOURCE_ADDRESS_BY_STORE = {
    "qifengz": {        # HUHOLE
        "name": "Xiao Wu",
        "company_name": "hangzhou muyufang kejiyouxiangongsi",
        "address_line1": "xinwankejichuangxinyuan 1-417shi",
        "city": "Hangzhou", "state_or_province_code": "Zhejiang",
        "country_code": "CN", "postal_code": "310000", "phone_number": "17767158681",
    },
    "serenorch": {      # Serenorch（玫玑研·上海主体）
        "name": "HongJun Sun",
        "company_name": "Mei Ji Yan Ri Yong Pin (Shanghai) Co., Ltd.",
        "address_line1": "suidelu 2nong 20hao 5ceng A503shi, putuoqu",
        "city": "Shanghai", "state_or_province_code": "Shanghai",
        "country_code": "CN", "postal_code": "200060", "phone_number": "19116409320",
    },
    "bfpeaky": {        # BFPeaky（周峰科技·杭州）
        "name": "Qifeng Zhou",
        "company_name": "hangzhouzhoufengkejiyouxiangongsi",
        "address_line1": "Xinwanjiedao, xinwankejichuangxinyuan2-206-2shi",
        "city": "Hangzhou", "state_or_province_code": "Zhejiang",
        "country_code": "CN", "postal_code": "311228", "phone_number": "17706538390",
    },
}
# 默认发货地址（未配置店铺时兜底）
SOURCE_ADDRESS = SOURCE_ADDRESS_BY_STORE["qifengz"]


def _source_address(store):
    """按店铺(amazon_store)取发货地址；未配置则用默认。"""
    return SOURCE_ADDRESS_BY_STORE.get((store or "").strip()) or SOURCE_ADDRESS

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


# ---------------------------------------------------------------- 来源：补仓计划 Excel / 赛狐采购计划

# 列关键词（归一化后按子串匹配）；声明顺序=认领顺序，先认领更专指的列
_COL_KEYS = [
    ("msku", ["amazon-sku", "amazonsku", "amazon sku", "msku", "sku"]),
    ("units_per_box", ["每箱商品数", "每箱数", "每箱", "units per box", "unitsperbox", "units/box"]),
    ("boxes", ["外箱数", "箱数", "number of boxes", "numberofboxes", "外箱", "boxes"]),
    ("quantity", ["补仓数量", "补货数量", "数量", "quantity", "qty"]),
    ("l_in", ["box length", "length", "长"]),
    ("w_in", ["box width", "width", "宽"]),
    ("h_in", ["box height", "height", "高"]),
    ("weight_lb", ["box weight", "weight", "重量", "重"]),
]


def _norm(s):
    return str(s or "").strip().lower().replace(" ", "")


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", ""))
        return int(f) if f == int(f) else round(f, 2)
    except (ValueError, TypeError):
        return None


def parse_replenishment_excel(content):
    """解析补仓计划 Excel → 建仓明细 raw_items。

    按表头关键词映射列：AMAZON-SKU/补仓数量/每箱商品数/外箱数/长/宽/高/重(IN/LB)。
    返回 [{msku,quantity,units_per_box,boxes,l_in,w_in,h_in,weight_lb}]；缺的留空（建仓时从产品库补/手填）。
    """
    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        raise RuntimeError("Excel 为空")

    # 找表头行：前 10 行里关键词命中最多的一行
    best_i, best_map, best_hits = 0, {}, -1
    for i, row in enumerate(rows[:10]):
        norm_cells = [_norm(c) for c in row]
        claimed = set()
        colmap = {}
        for field, keys in _COL_KEYS:
            for ci, cell in enumerate(norm_cells):
                if ci in claimed or not cell:
                    continue
                if any(k.replace(" ", "") in cell for k in keys):
                    colmap[field] = ci
                    claimed.add(ci)
                    break
        hits = len(colmap)
        if hits > best_hits:
            best_i, best_map, best_hits = i, colmap, hits

    if "msku" not in best_map or "quantity" not in best_map:
        raise RuntimeError("未识别到 SKU / 数量列，请确认补仓计划表头（需含 AMAZON-SKU、补仓数量等）")

    items = []
    for row in rows[best_i + 1:]:
        msku = str(row[best_map["msku"]] or "").strip() if best_map.get("msku") is not None else ""
        qty = _num(row[best_map["quantity"]]) if best_map.get("quantity") is not None else None
        if not msku or not qty:
            continue
        it = {"msku": msku, "quantity": int(qty)}
        for f in ("units_per_box", "boxes", "l_in", "w_in", "h_in", "weight_lb"):
            ci = best_map.get(f)
            it[f] = _num(row[ci]) if ci is not None else None
        items.append(it)
    if not items:
        raise RuntimeError("补仓计划没有有效明细行")
    return items


def items_from_purchase_plan(db, plan_group_no):
    """从赛狐采购计划取建仓明细（SKU+采购量）；箱规留空（建仓时从产品库补）。"""
    from . import purchase_plan_service as pps
    grp = pps._find_group(db, plan_group_no)
    if grp is None:
        raise RuntimeError(f"采购计划 {plan_group_no} 未找到")
    items = []
    for it in grp.get("purchasePlanItemVoList") or []:
        msku = str(it.get("msku") or it.get("sku") or "").strip()
        qty = _num(it.get("planNum"))
        if not msku or not qty:
            continue
        items.append({"msku": msku, "quantity": int(qty),
                      "units_per_box": _num(it.get("cartonQty")),
                      "boxes": _num(it.get("cartonNum")),
                      "l_in": None, "w_in": None, "h_in": None, "weight_lb": None})
    brand_name = ""
    for it in grp.get("purchasePlanItemVoList") or []:
        if it.get("brandName"):
            brand_name = it["brandName"]
            break
    return {"items": items, "brand_name": brand_name,
            "name": _norm_plan_name(grp)}


def _norm_plan_name(grp):
    its = grp.get("purchasePlanItemVoList") or []
    shop = (its[0].get("shopName") if its else "") or ""
    site = (its[0].get("siteName") if its else "") or ""
    return f"{shop or grp.get('planGroupNo','')}-{site}".strip("-")


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
    src = _source_address(store)
    resp = fba.call("POST", "/inbound-plans", store=store, timeout=90,
                    json={"name": name or None, "source_address": src, "items": api_items})
    plan_id = resp.get("inboundPlanId", "")
    op = resp.get("operationId", "")
    rec = InboundPlan(
        source_type=source_type, source_ref=source_ref, brand_id=brand_id, store=store or "",
        name=name, status=ST_CREATED, amazon_inbound_plan_id=plan_id, current_operation_id=op,
        items_snapshot=json.dumps(items, ensure_ascii=False),
        source_address=json.dumps(src, ensure_ascii=False))
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


def _default_expiration():
    """效期（YYYY-MM-DD）。部分 SKU（亚马逊标记需效期）建仓必须带，且亚马逊要求
    效期 ≥ 今天+105 天（FBA_INB_0181）。面巾等长保质期商品取 +1 年，安全且合规。"""
    from datetime import date, timedelta
    return (date.today() + timedelta(days=365)).isoformat()


def build_for_batch(db, batch):
    """直接给批次建仓（不走向导）：用批次明细跑 create→packing→boxes→placement，
    把分仓方案(含合仓)算好箱数/重量存到 batch.placement_options，并回填 inbound_plan_id。

    箱规/数量取批次明细的产品箱规；store 按品牌 amazon_store（空=默认 main）。
    """
    from ..models import Batch  # 避免顶层循环引用
    raw = [{"msku": it.msku, "quantity": it.qty}
           for sp in batch.shipments for it in sp.items if (it.qty or 0) > 0]
    if not raw:
        raise RuntimeError("批次无明细，无法建仓")
    items = _resolve_items(db, raw)              # 校验+补箱规（缺箱规会在这里报错）
    store = _store(db, batch.brand_id) or None
    spec = {it["msku"]: it for it in items}

    api_items = [{"msku": it["msku"], "quantity": it["quantity"],
                  "prep_owner": "SELLER", "label_owner": "SELLER"} for it in items]

    def _create():
        return fba.call("POST", "/inbound-plans", store=store, timeout=90,
                        json={"name": batch.name, "source_address": _source_address(store), "items": api_items})

    # 部分 SKU（无标/Amazon贴标）只接受 labelOwner/prepOwner=NONE：亚马逊逐条报
    # "ERROR: <MSKU> does not require labelOwner ... Accepted values: [NONE]"。
    # 据错误把对应 SKU 改成 NONE 重试（多个 SKU、label+prep 都可能，故循环几轮）。
    # 校验既可能同步拒绝(create 502)，也可能异步失败(create 返回 operationId 后
    # wait_operation FAILED，如效期)。故 create+wait 一起放进重试：据错误把 SKU 的
    # labelOwner/prepOwner 改 NONE、或补效期(今天+1月)，再重试；重试前取消已建的草稿计划避免残留。
    resp = None
    last_err = ""
    for _ in range(4):
        pid_try = None
        try:
            r = _create()
            pid_try = r.get("inboundPlanId", "")
            if r.get("operationId"):
                fba.wait_operation(r["operationId"], store=store, timeout=120)
            resp = r
            break
        except RuntimeError as e:
            last_err = str(e)
            if pid_try:                       # 取消刚建但校验失败的草稿计划
                try:
                    fba.call("PUT", f"/inbound-plans/{pid_try}/cancel", store=store, timeout=60)
                except Exception:
                    pass
            none_label = set(re.findall(r"ERROR:\s*(\S+)\s+does not require labelOwner", last_err))
            none_prep = set(re.findall(r"ERROR:\s*(\S+)\s+does not require prepOwner", last_err))
            # 需效期的 SKU：FBA_INB_0180 "resource '<MSKU>' ... Expiration date required"
            need_exp = (set(re.findall(r"resource '([^']+)'", last_err))
                        if "Expiration date required" in last_err else set())
            changed = False
            for ai in api_items:
                if ai["msku"] in none_label and ai["label_owner"] != "NONE":
                    ai["label_owner"] = "NONE"
                    changed = True
                if ai["msku"] in none_prep and ai["prep_owner"] != "NONE":
                    ai["prep_owner"] = "NONE"
                    changed = True
                if ai["msku"] in need_exp and not ai.get("expiration"):
                    ai["expiration"] = _default_expiration()   # 今天+1年(≥105天合规)
                    changed = True
            if not changed:                 # 不是可自动修的 owner/效期 问题 → 停
                break
    if resp is None:
        acct = store or "main(默认)"
        if "not valid" in last_err or "MSKU" in last_err or "BadRequest" in last_err:
            raise RuntimeError(
                f"建仓账户=「{acct}」拒绝了这些 SKU（多半是该品牌没映射到正确的亚马逊账户）。"
                f"请在「主体与品牌」给品牌设置 amazon_store、并确保该账户凭据已配进 mcapi。原始：{last_err[:200]}")
        raise RuntimeError(last_err)
    pid = resp.get("inboundPlanId", "")
    batch.inbound_plan_id = pid
    db.commit()

    g = fba.call("POST", f"/inbound-plans/{pid}/packing-options/generate", store=store)
    fba.wait_operation(g["operationId"], store=store)
    po = ((fba.call("GET", f"/inbound-plans/{pid}/packing-options", store=store) or {}).get("packingOptions") or [None])[0]
    if not po:
        raise RuntimeError("没有可用装箱方案")
    c = fba.call("POST", f"/inbound-plans/{pid}/packing-options/{po['packingOptionId']}/confirm", store=store)
    fba.wait_operation(c["operationId"], store=store)

    # 箱内 item 必须与入库计划的 item 完全一致（labelOwner/prepOwner/expiration），
    # 否则 set_packing_information 报 "did not contain expected items"。从最终 api_items 取。
    owner_map = {}
    for ai in api_items:
        d = {"prepOwner": ai.get("prep_owner", "SELLER"),
             "labelOwner": ai.get("label_owner", "SELLER")}
        if ai.get("expiration"):
            d["expiration"] = ai["expiration"]
        owner_map[ai["msku"]] = d

    boxes = []
    for it in items:
        upb = it["units_per_box"]
        full, rem = divmod(it["quantity"], upb)
        dims = {"unitOfMeasurement": "IN", "length": it["l_in"], "width": it["w_in"], "height": it["h_in"]}
        wt = {"unit": "LB", "value": it["weight_lb"]}
        line = {"msku": it["msku"],
                **owner_map.get(it["msku"], {"prepOwner": "SELLER", "labelOwner": "SELLER"})}
        if full > 0:
            boxes.append({"contentInformationSource": "BOX_CONTENT_PROVIDED",
                          "items": [dict(line, quantity=upb)], "dimensions": dims, "weight": wt, "quantity": full})
        if rem > 0:
            boxes.append({"contentInformationSource": "BOX_CONTENT_PROVIDED",
                          "items": [dict(line, quantity=rem)], "dimensions": dims, "weight": wt, "quantity": 1})
    r = fba.call("POST", f"/inbound-plans/{pid}/packing-information", store=store,
                 json={"package_groupings": [{"packingGroupId": (po.get("packingGroups") or [None])[0], "boxes": boxes}]})
    fba.wait_operation(r["operationId"], store=store)

    g2 = fba.call("POST", f"/inbound-plans/{pid}/placement-options/generate", store=store)
    fba.wait_operation(g2["operationId"], store=store, timeout=300)
    pls = fba.call("GET", f"/inbound-plans/{pid}/placement-options", store=store) or {}

    out = []
    for o in pls.get("placementOptions") or []:
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
            si = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sid}/items", store=store) or {}
            by, units, bx, wkg = {}, 0, 0, 0.0
            for x in si.get("items") or []:
                m = x.get("msku"); q = int(x.get("quantity") or 0)
                by[m] = by.get(m, 0) + q; units += q
                sp_it = spec.get(m)
                if sp_it and sp_it["units_per_box"]:
                    nb = math.ceil(q / sp_it["units_per_box"])
                    bx += nb; wkg += nb * (sp_it["weight_lb"] / LB_PER_KG)
            ships.append({"fc": dest.get("warehouseId"), "city": addr.get("city"),
                          "state": addr.get("stateOrProvinceCode"), "units": units,
                          "boxes": bx, "weight_kg": round(wkg, 1), "by_sku": by})
        out.append({"placement_option_id": o.get("placementOptionId"),
                    "label": f"{len(o.get('shipmentIds') or [])} 仓",
                    "fee_usd": round(fees, 2), "shipments": ships})
    batch.placement_options = json.dumps(out, ensure_ascii=False)
    batch.status = "已建仓"          # 建仓出分仓方案 → 标记已建仓（进度卡据此给按钮）
    db.commit()
    return {"inbound_plan_id": pid, "option_count": len(out)}


def materialize_placement(db, batch, placement_option_id):
    """把选中分仓方案落成逐FC Shipment+ShipmentItem行（供生成文件，不碰亚马逊）。

    各仓 SKU/数量取方案 by_sku；明细成本/箱规从原"未分仓"货件同SKU行复制。
    保护：批次已是逐FC货件（fc_code 非空，如导入的真实批次）则跳过，绝不删真实数据。
    """
    import math as _math
    from ..models import Shipment, ShipmentItem
    already = [sp for sp in batch.shipments if (sp.fc_code or "").strip()]
    if already:
        return {"skipped": True, "reason": "已是逐FC货件",
                "fcs": [sp.fc_code for sp in already]}
    opts = json.loads(batch.placement_options or "[]")
    opt = next((o for o in opts if o.get("placement_option_id") == placement_option_id), None)
    if not opt:
        raise RuntimeError(f"分仓方案 {placement_option_id} 不存在")
    tmpl = {}                              # 原货件SKU模板（复制规格）
    for sp in batch.shipments:
        for it in sp.items:
            tmpl.setdefault(it.msku, it)
    spec_cols = ("product_id", "fnsku", "asin", "qty_per_box",
                 "carton_l", "carton_w", "carton_h", "customs_unit_price", "purchase_cost")
    for sp in list(batch.shipments):       # 清掉原"未分仓"货件
        db.delete(sp)
    db.flush()
    made = []
    for sh in opt.get("shipments") or []:
        fc = sh.get("fc")
        nsp = Shipment(batch_id=batch.id, fc_code=fc, status="已分仓")
        db.add(nsp)
        db.flush()
        for msku, qty in (sh.get("by_sku") or {}).items():
            ni = ShipmentItem(shipment_id=nsp.id, msku=msku, qty=qty)
            src = tmpl.get(msku)
            if src:
                for c in spec_cols:
                    setattr(ni, c, getattr(src, c, None))
                if ni.qty_per_box:
                    ni.box_count = _math.ceil((qty or 0) / ni.qty_per_box)
            db.add(ni)
        made.append({"fc": fc, "skus": len(sh.get("by_sku") or {})})
    db.commit()
    return {"materialized": made}


def confirm_placement_for_batch(db, batch, placement_option_id, *, live=False):
    """选定后：对批次确认某分仓方案 → 生成 FC 货件 → 配自送运输 → 回填货件。

    **live=False（默认）只演练(dry-run)**：构造将执行的步骤，**完全不调用亚马逊**。
    live=True 才真实写亚马逊（Feishu 侧由环境变量 INBOUND_LIVE_SUBMIT=1 才允许）。
    """
    pid = batch.inbound_plan_id
    if not pid:
        raise RuntimeError("该批次还没建仓(无 inbound_plan_id)，先建仓")
    try:
        opts = json.loads(batch.placement_options or "[]")
    except (ValueError, TypeError):
        opts = []
    chosen = next((o for o in opts if o.get("placement_option_id") == placement_option_id), None)
    if not chosen:
        raise RuntimeError(f"分仓方案 {placement_option_id} 不在该批次方案里")
    store = _store(db, batch.brand_id) or None
    fcs = [s.get("fc") for s in (chosen.get("shipments") or [])]
    plan = {"inbound_plan_id": pid, "store": store or "main(默认)",
            "placement_option_id": placement_option_id, "fcs": fcs,
            "steps": ["确认分仓方案(生成货件)", "生成运输方案(自送)",
                      "选自送承运人并确认", "回填货件 FC/确认号"]}
    # 回填逐FC货件行（本地，不碰亚马逊）→ 让后续"生成文件"有数据
    try:
        plan["materialized"] = materialize_placement(db, batch, placement_option_id)
    except Exception as e:
        plan["materialize_error"] = str(e)[:120]
    if not live:
        plan["dry_run"] = True
        return plan                       # 🛑 演练：到此为止，不碰亚马逊
    # ===== 真实写亚马逊：每步幂等（已做过则忽略"already/cannot be processed"继续）=====
    # 1) 确认分仓方案（已确认则跳过）
    try:
        c = fba.call("POST", f"/inbound-plans/{pid}/placement-options/{placement_option_id}/confirm",
                     store=store)
        if c.get("operationId"):
            fba.wait_operation(c["operationId"], store=store, timeout=300)
    except RuntimeError as e:
        if not _already_done(e):
            raise
    # 2) 取该方案的货件
    pl = fba.call("GET", f"/inbound-plans/{pid}/placement-options", store=store) or {}
    o = next((x for x in pl.get("placementOptions") or []
              if x.get("placementOptionId") == placement_option_id), None)
    shipment_ids = (o.get("shipmentIds") if o else []) or []
    if not shipment_ids:
        raise RuntimeError("确认分仓后未取到货件(检查方案/计划状态)")
    # 3) 自送运输（幂等）+ 回填
    _config_self_delivery(pid, placement_option_id, shipment_ids, store)
    ships = _backfill_shipments(batch, pid, shipment_ids, store)
    batch.status = "运输已配置"
    db.commit()
    plan.update({"dry_run": False, "shipments": ships})
    return plan


def _already_done(e):
    """判断亚马逊报错是不是"该步已做过/状态已变"——幂等流程里这种忽略继续。"""
    s = str(e).lower()
    return any(k in s for k in ("already", "has already been confirmed",
                                "cannot be processed", "already confirmed"))


def _config_self_delivery(pid, placement_option_id, shipment_ids, store):
    """给各货件配自送(非合作承运)运输：generate → 选 USE_YOUR_OWN_CARRIER → confirm。"""
    import time
    from datetime import date, timedelta
    # readyToShipWindow = 这周的周五再向后推 50 天。实测窗口太近时部分货件出不了 OTHER(自有承运)
    # 选项；推远后基本都有。所有货件都用 OTHER → 同一承运、不会 carrier 不一致(FBA_INB_0354)。
    _today = date.today()
    _friday = _today + timedelta(days=(4 - _today.weekday()) % 7)   # 这周周五(今天周五则今天)
    rtw = {"start": (_friday + timedelta(days=50)).strftime("%Y-%m-%dT00:00:00Z")}
    cfgs = [{"shipmentId": sid, "readyToShipWindow": rtw} for sid in shipment_ids]
    # 多次采样：OTHER 选项时有时无 → 每次重新 generate，按 shipmentId 逐货件取 OTHER LTL，
    # 直到所有货件都拿到 OTHER(按 placementOptionId 查只返回第一个货件，必须逐货件查)。
    selections, missing = [], list(shipment_ids)
    for _ in range(8):
        g = fba.call("POST", f"/inbound-plans/{pid}/transportation-options/generate", store=store,
                     json={"placement_option_id": placement_option_id,
                           "shipment_transportation_configurations": cfgs})
        fba.wait_operation(g["operationId"], store=store, timeout=300)
        selections, missing = [], []
        for sid in shipment_ids:
            opts = fba.call("GET", f"/inbound-plans/{pid}/transportation-options",
                            params={"shipment_id": sid, "page_size": 20}, store=store) or {}
            chosen = _pick_self_delivery(opts.get("transportationOptions") or [], sid)
            if chosen:
                selections.append({"shipmentId": sid, "transportationOptionId": chosen})
            else:
                missing.append(sid)
        if not missing:
            break
        time.sleep(3)
    if missing:
        raise RuntimeError(f"多次采样后仍有货件无 OTHER 自送 LTL 选项: {missing}")
    # 自送(非合作承运)货件：确认运输前必须先确认每个货件的"送仓时间窗"（幂等）
    for sid in shipment_ids:
        try:
            g2 = fba.call("POST", f"/inbound-plans/{pid}/shipments/{sid}/delivery-window-options/generate",
                          store=store)
            if g2.get("operationId"):
                fba.wait_operation(g2["operationId"], store=store, timeout=300)
            dwo = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sid}/delivery-window-options",
                           store=store) or {}
            windows = dwo.get("deliveryWindowOptions") or dwo.get("deliveryWindowOptionsList") or []
            if not windows:
                raise RuntimeError(f"货件 {sid} 无可选送仓时间窗(delivery window)")
            wid = windows[0].get("deliveryWindowOptionId") or windows[0].get("id")
            cf2 = fba.call("POST",
                           f"/inbound-plans/{pid}/shipments/{sid}/delivery-window-options/{wid}/confirm",
                           store=store)
            if cf2.get("operationId"):
                fba.wait_operation(cf2["operationId"], store=store, timeout=300)
        except RuntimeError as e:
            if not _already_done(e):
                raise
    try:
        cf = fba.call("POST", f"/inbound-plans/{pid}/transportation-options/confirm", store=store,
                      json={"selections": selections})
        fba.wait_operation(cf["operationId"], store=store, timeout=300)
    except RuntimeError as e:
        if not _already_done(e):
            raise


SELF_DELIVERY_MODE = "FREIGHT_LTL"   # 自送只用整车/托盘(LTL)，不用小包(SPD)


def _is_other_carrier(c):
    """承运商是否为 OTHER(自有承运/不在列表)——alphaCode 为空或名称为 Other。"""
    c = c or {}
    return c.get("alphaCode") in (None, "", "OTHER") or (c.get("name") or "").strip().upper() == "OTHER"


def _pick_self_delivery(all_opts, shipment_id, mode=SELF_DELIVERY_MODE):
    """挑该货件的自送 LTL + **承运商 OTHER(自有承运)** 选项。只用 LTL、不用 SPD 小包。
    所有货件都用 OTHER → 同一承运商，避免 FBA_INB_0354(carrier 不一致)。无则返回 None。"""
    ltl_other = [o for o in all_opts if o.get("shipmentId") == shipment_id
                 and "OWN" in (o.get("shippingSolution") or "").upper()
                 and o.get("shippingMode") == mode and _is_other_carrier(o.get("carrier"))]
    return ltl_other[0].get("transportationOptionId") if ltl_other else None


def fetch_labels(db, batch, kind="box", page_type=None, live=False):
    """下载并剪切标签：kind=box 箱唛 / fnsku 商品标签。多合一页按页型切成单张供货代。

    **live=False（默认）演练，不碰亚马逊**；live=True 走 mcapi 取下载链接→下载→剪切→归档。
    """
    from ..database import OUTPUT_DIR
    from ..modules import label_tools as lt
    pid = batch.inbound_plan_id
    if not pid:
        raise RuntimeError("批次未建仓（无 inbound_plan_id）")
    store = _store(db, batch.brand_id) or None
    page_type = page_type or ("PackageLabel_Letter_6" if kind == "box" else "LETTER_30")
    plan = {"batch": batch.name, "kind": kind, "page_type": page_type, "inbound_plan_id": pid,
            "steps": ["取标签下载链接", "下载 PDF", "按页型剪切成单张", "归档"]}
    if not live:
        plan["dry_run"] = True
        return plan                       # 🛑 演练：到此为止，不碰亚马逊
    out_dir = os.path.join(OUTPUT_DIR, re.sub(r'[\\/:*?"<>|]', "_", batch.name or f"batch{batch.id}"), "labels")
    os.makedirs(out_dir, exist_ok=True)
    saved = []

    def _url(resp):
        resp = resp or {}
        return resp.get("DownloadURL") or resp.get("downloadURL") or resp.get("url")

    if kind == "box":
        p = fba.call("GET", f"/inbound-plans/{pid}", store=store) or {}
        for sh in (p.get("shipments") or []):
            conf = sh.get("shipmentConfirmationId")
            if not conf:
                continue
            boxes = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sh.get('shipmentId')}/boxes",
                             store=store) or {}
            n_boxes = len(boxes.get("boxes") or []) or None
            params = {"PageType": page_type, "LabelType": "BARCODE_2D"}
            if n_boxes:                    # 非合作承运(自送)货件 PageSize 必填
                params["PageSize"] = n_boxes
                params["PageStartIndex"] = 0
            url = _url(fba.call("GET", f"/shipments/{conf}/labels", params=params, store=store))
            if not url:
                continue
            raw = os.path.join(out_dir, f"{conf}_原始.pdf")
            cut = os.path.join(out_dir, f"{conf}_单张.pdf")
            lt.download_pdf(url, raw)
            n = lt.cut_by_page_type(raw, cut, page_type)
            saved.append({"shipment": conf, "boxes": n_boxes, "cut_pdf": cut, "labels": n})
    else:  # fnsku
        items = [{"msku": it.msku, "quantity": it.qty}
                 for sp in batch.shipments for it in sp.items if (it.qty or 0) > 0]
        url = _url(fba.call("POST", "/item-labels", store=store,
                            json={"label_type": "STANDARD_FORMAT", "page_type": page_type,
                                  "msku_quantities": items}))
        if url:
            raw = os.path.join(out_dir, "fnsku_原始.pdf")
            cut = os.path.join(out_dir, "fnsku_单张.pdf")
            lt.download_pdf(url, raw)
            n = lt.cut_by_page_type(raw, cut, page_type)
            saved.append({"cut_pdf": cut, "labels": n})
    # 文件多 → 打包成一个 zip（各货件箱唛 + FNSKU + 清单）+ 合并一个 PDF 便于一次打印
    cut_files = [s["cut_pdf"] for s in saved if s.get("cut_pdf")]
    zip_path = merged = None
    total = sum(s.get("labels") or 0 for s in saved)
    if cut_files:
        manifest = [f"{s.get('shipment', 'FNSKU')}  {s.get('labels')} 张  "
                    f"{os.path.basename(s.get('cut_pdf', ''))}" for s in saved]
        manifest.append(f"合计 {total} 张")
        from ..modules import label_tools as lt
        zip_path = lt.bundle_zip(cut_files, os.path.join(out_dir, f"{kind}_标签打包.zip"), manifest)
        merged, _ = lt.merge_pdfs(cut_files, os.path.join(out_dir, f"{kind}_全部单张.pdf"))
    plan.update({"dry_run": False, "saved": saved, "out_dir": out_dir,
                 "zip": zip_path, "merged_pdf": merged, "total_labels": total})
    return plan


def _backfill_shipments(batch, pid, shipment_ids, store):
    """读确认后的 FC 货件(fc/地址/确认号)返回（持久化到 Shipment 留给发托书环节）。"""
    out = []
    for sid in shipment_ids:
        sh = fba.call("GET", f"/inbound-plans/{pid}/shipments/{sid}", store=store) or {}
        dest = sh.get("destination", {}) or {}
        addr = dest.get("address", {}) or {}
        out.append({"shipmentId": sid, "fc": dest.get("warehouseId"),
                    "confirmationId": sh.get("shipmentConfirmationId"),
                    "city": addr.get("city"), "state": addr.get("stateOrProvinceCode"),
                    "status": sh.get("status")})
    return out
