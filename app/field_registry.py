"""字段字典 —— 模板映射的唯一取值入口（CLAUDE.md 约定 1）。

- FIELD_DICT：命名空间 → {key: 中文标签}，前端映射配置面板照此展示；
  模板映射只能引用这里登记的路径，新增字段先登记再使用。
- build_context(db, batch, shipment=None)：把一个批次/货件的全部取值
  组装成 dict；shipment=None 时为批次级上下文（含 shipments 行列表
  供 row_per_shipment 模板使用，以及 items_agg=跨货件按 msku 聚合、
  plan_items=每 (货件×SKU) 一行两个明细区数据源）。
- resolve(path, ctx, row=None)：分级取值；"text:" 前缀返回字面量；
  含 {} 视为格式串（{path} 替换，None→空串）。
"""

import re
from datetime import date, datetime

from . import rule_engine
from .models import Company

IN_TO_CM = 2.54
LB_TO_KG = 0.4536

# ---------------------------------------------------------------- 字段字典

FIELD_DICT = {
    "batch": {
        "name": "批次名",
        "brand": "品牌名",
        "country": "国家",
        "contract_date": "合同日期",
        "contract_date_dt": "合同日期(日期对象)",
        "base_date": "发货基准日(周五)",
    },
    "shipment": {
        "amazon_shipment_id": "FBA货件号",
        "reference_id": "Reference ID",
        "ship_sn": "发货单号",
        "fc_code": "目的仓代码",
        "address_line1": "仓库地址1",
        "address_line2": "仓库地址2",
        "city": "城市",
        "state": "州",
        "postal_code": "邮编",
        "country_code": "国家代码",
        "carton_num": "箱数",
        "total_gross_weight": "总毛重(kg)",
        "total_net_weight": "总净重(kg)",
        "total_volume": "总体积(m³)",
        "total_qty": "总数量",
        "total_value": "总申报金额",
        "total_purchase_value": "总采购货值(Σ数量×采购成本)",
        "total_contract_value": "总合同货值(Σ数量×成本×合同系数,不舍入)",
        "total_qty_float": "总数量(浮点串,如512.0,复刻RPA口径)",
        "forwarder_order_no": "货代单号",
    },
    "item": {
        "seq": "序号",
        "product_id": "产品ID",
        "product_sort": "产品导入行序(当周计划行序)",
        "msku": "MSKU",
        "fnsku": "FNSKU",
        "asin": "ASIN",
        "name_customs_cn": "中文报关名",
        "name_customs_en": "英文报关名",
        "name_contract": "合同用品名",
        "name_invoice": "发票箱单品名",
        "name_usage": "用途功能品名",
        "hs_code": "HS编码",
        "declare_elements": "申报要素",
        "material": "材质",
        "material_en": "英文材质",
        "usage": "用途",
        "brand": "品牌(报关)",
        "model": "型号",
        "qty": "数量",
        "box_count": "箱数",
        "qty_per_box": "每箱数量",
        "carton_l": "箱规长(cm)",
        "carton_w": "箱规宽(cm)",
        "carton_h": "箱规高(cm)",
        "carton_l_in": "箱规长(in)",
        "carton_w_in": "箱规宽(in)",
        "carton_h_in": "箱规高(in)",
        "carton_lwh_in": "箱规长×宽×高乘积(in)",
        "box_weight_kg": "单箱重量(kg)",
        "total_gross_weight": "总毛重(单箱重×箱数)",
        "declare_full": "申报要素串(用途|材质|品牌|规格)",
        "customs_unit_price": "申报单价",
        "purchase_cost": "采购成本",
        "amount": "申报金额(数量×单价)",
        "purchase_amount": "采购金额(数量×采购成本)",
        "image_url": "产品图片",
    },
    "box": {
        "box_no": "箱号(序号)",
        "box_no_text": "箱号(n/总)",
        "box_id": "亚马逊箱号",
        "msku": "内装MSKU",
        "qty": "内装数量",
        "length_in": "长(in)",
        "width_in": "宽(in)",
        "height_in": "高(in)",
        "length_cm": "长(cm)",
        "width_cm": "宽(cm)",
        "height_cm": "高(cm)",
        "weight_lb": "箱重(lb)",
        "weight_kg": "箱重(kg)",
    },
    "company": {
        "name_cn": "店铺主体中文名",
        "name_en": "店铺主体英文名",
        "address_cn": "中文地址",
        "address_en": "英文地址",
        "abbr": "缩写",
        "uscc": "统一社会信用代码",
        "customs_code": "海关编码",
        "phone": "电话",
        "contact": "联系人",
        "bank_info": "银行信息",
        "insurance_factor": "保价倍率",
        "default_port": "默认起运港",
        "default_origin_place": "默认货源地",
    },
    "factory": {
        "name": "工厂名称",
        "abbr": "工厂缩写",
        "address": "工厂地址",
        "phone": "工厂电话",
        "contact": "工厂联系人",
        "tax_no": "工厂税号",
        "bank_info": "工厂银行信息",
    },
    "trade": {
        "name_cn": "外贸主体中文名",
        "name_en": "外贸主体英文名",
        "address_cn": "外贸主体中文地址",
        "address_en": "外贸主体英文地址",
        "abbr": "外贸主体缩写",
        "uscc": "外贸主体信用代码",
        "customs_code": "外贸主体海关编码",
        "phone": "外贸主体电话",
        "contact": "外贸主体联系人",
        "bank_info": "外贸主体银行信息",
    },
    "forwarder": {
        "name": "货代名称",
        "contact": "货代联系人",
        "phone": "货代电话",
    },
    "calc": {
        "doc_no": "货件级合同编号",
        "purchase_no": "采购合同编号",
        "price_vat": "含税单价(成本×1.13)",
        "amount_vat": "含税金额",
        "price_contract": "采购合同单价(成本×合同系数,不舍入)",
        "amount_contract": "采购合同金额(数量×合同单价,不舍入)",
        "price_vat_markup": "二级加价单价(×1.13×1.1)",
        "amount_vat_markup": "二级加价金额",
        "insurance_price": "保价单价",
        "insurance_amount": "保价金额",
        "contract_date_cn": "合同日期(中文)",
        # 报关/投保常量（RuleConfig 可改）
        "origin_country": "原产国",
        "dest_country": "运抵国",
        "unit_name": "申报计量单位",
        "currency_customs": "报关币制",
        "package_type": "包装种类",
        "supervision_mode": "监管方式",
        "tax_mode": "征免性质",
        "transport_mode": "运输方式",
        "delivery_mode": "派送方式",
        "dest_type": "目的地类型",
        "shelf_guarantee": "上架保障",
        "departure_port": "起运港",
        "fragile": "易碎品",
        "insurance_currency": "保险币种",
        "insurance_ratio": "投保比例",
    },
    "meta": {
        "today": "今天(YYYY-MM-DD)",
        "today_slash": "今天(YYYY/MM/DD)",
        "today_mmdd": "今天(MMDD)",
        "today_compact": "今天(YYYYMMDD)",
        "now_compact": "当前时间(YYYYMMDDHHMMSS)",
        "base_friday": "基准周五(YYYY-MM-DD)",
        "base_friday_slash": "基准周五(YYYY/MM/DD)",
        "base_friday_mmdd": "基准周五(MMDD)",
        "base_friday_mm_dd": "基准周五(MM-DD)",
        "base_friday_mm_dot_dd": "基准周五(MM.DD)",
        "base_friday_dt": "基准周五(日期对象)",
    },
}


# ---------------------------------------------------------------- 内部工具

def _attr_dict(obj, keys):
    if obj is None:
        return {k: None for k in keys}
    return {k: getattr(obj, k, None) for k in keys}


def _round(v, n):
    return round(v, n) if v is not None else None


def _cn_date(d):
    """'2026-05-19' → '2026年5月19日'。"""
    try:
        dt = rule_engine._to_date(d)
    except (ValueError, TypeError):
        return str(d or "")
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _cm_to_in(v):
    """箱规 cm → in（÷2.54，圆整 2 位；54cm → 21.26in）。"""
    return round(v / IN_TO_CM, 2) if v is not None else None


def _declare_full(p):
    """报关单明细行申报要素串（对照跨运通 RPA process2/3.py「报关」块 64/66）：

    '用途：{usage}|材质：{material}|品牌：{brand_name}'，有型号时追加 '|规格：{model}'
    （RPA 用「规格」做标签且无型号时整段省略，照抄保持逐单元格一致）。
    """
    if p is None:
        return ""
    s = (f"用途：{p.usage or ''}|材质：{p.material or ''}"
         f"|品牌：{p.brand_name or ''}")
    if (p.model or "").strip():
        s += f"|规格：{p.model}"
    return s


def _item_row(it, seq, db, company):
    """明细行 → {"item": {...}, "calc": {...}}（calc 按行，基于本行成本/单价）。"""
    p = it.product
    qty = it.qty or 0
    price = it.customs_unit_price
    if price is None and p is not None:
        price = p.unit_price_default
    cost = it.purchase_cost
    if cost is None and p is not None:
        cost = p.purchase_cost_default
    cid = company.id if company is not None else None

    # 箱规优先级：①亚马逊逐箱实测(in，建仓时物理申报值，最权威——赛狐商品资料可能录错，
    # 如 FT-1 箱高赛狐=77cm 实际=44cm) ②赛狐明细(cm) ③产品主数据(cm)
    _box = next((b for b in (it.shipment.boxes if it.shipment is not None else [])
                 if b.msku == it.msku and b.height_in is not None
                 and b.length_in is not None), None)
    if _box is not None:
        carton_l = _round(_box.length_in * 2.54, 1)
        carton_w = _round(_box.width_in * 2.54, 1)
        carton_h = _round(_box.height_in * 2.54, 1)
        carton_in = (_box.length_in, _box.width_in, _box.height_in)
    else:
        carton_l = it.carton_l if it.carton_l is not None else (p.carton_l_cm if p else None)
        carton_w = it.carton_w if it.carton_w is not None else (p.carton_w_cm if p else None)
        carton_h = it.carton_h if it.carton_h is not None else (p.carton_h_cm if p else None)
        carton_in = (_cm_to_in(carton_l), _cm_to_in(carton_w), _cm_to_in(carton_h))
    qty_per_box = it.qty_per_box if it.qty_per_box is not None \
        else (p.qty_per_box if p else None)

    item = {
        "seq": seq,
        "product_id": (p.id if p is not None else None),
        "product_sort": (getattr(p, "sort_index", None) if p is not None else None),
        "msku": it.msku or "",
        "fnsku": it.fnsku or "",
        "asin": it.asin or "",
        "name_customs_cn": p.name_customs_cn if p else "",
        "name_customs_en": p.name_customs_en if p else "",
        # 合同/发票品名历史规律=「SKU（中文报关名）」，用途品名=中文报关名——
        # 产品库没填时自动推导，避免"品名匹配不到"
        "name_contract": (p.name_contract or (
            f"{p.sku}（{p.name_customs_cn}）" if p.name_customs_cn else "")) if p else "",
        "name_invoice": (p.name_invoice or (
            f"{p.sku}（{p.name_customs_cn}）" if p.name_customs_cn else "")) if p else "",
        "name_usage": (p.name_usage or p.name_customs_cn) if p else "",
        "hs_code": p.hs_code if p else "",
        "declare_elements": (p.declare_elements or "") if p else "",
        "material": p.material if p else "",
        "material_en": getattr(p, "material_en", "") if p else "",
        "usage": p.usage if p else "",
        "brand": p.brand_name if p else "",
        "model": p.model if p else "",
        "qty": qty,
        "box_count": it.box_count,
        "qty_per_box": qty_per_box,
        "carton_l": carton_l,
        "carton_w": carton_w,
        "carton_h": carton_h,
        "carton_l_in": carton_in[0],
        "carton_w_in": carton_in[1],
        "carton_h_in": carton_in[2],
        # 长×宽×高(in) 乘积——HUHOLE/BFPeaky 跨运通托书 C 列口径（RPA 将
        # 三个英寸尺寸直接相乘写入，保留浮点全精度）
        "carton_lwh_in": (carton_in[0] * carton_in[1] * carton_in[2]
                          if None not in carton_in else None),
        "box_weight_kg": p.box_weight_kg if p else None,
        "total_gross_weight": (
            _round(p.box_weight_kg * it.box_count, 2)
            if p is not None and p.box_weight_kg is not None
            and it.box_count is not None else None),
        "declare_full": _declare_full(p),
        "customs_unit_price": price,
        "purchase_cost": cost,
        "amount": _round(qty * price, 2) if price is not None else None,
        "purchase_amount": _round(qty * cost, 2) if cost is not None else None,
        "image_url": (p.image_url or "") if p else "",
    }
    pv = rule_engine.price_vat(cost, db, cid)
    pvm = rule_engine.price_vat_markup(cost, db, cid)
    pc = rule_engine.price_contract(cost, db, cid)
    ins = rule_engine.insurance_price(price, company)
    calc = {
        "price_vat": pv,
        "amount_vat": _round(pv * qty, 2) if pv is not None else None,
        "price_vat_markup": pvm,
        "amount_vat_markup": _round(pvm * qty, 2) if pvm is not None else None,
        # 采购合同口径：全精度不舍入（成品如 36.62895）
        "price_contract": pc,
        "amount_contract": (pc * qty) if pc is not None else None,
        "insurance_price": ins,
        "insurance_amount": _round(ins * qty, 2) if ins is not None else None,
    }
    return {"item": item, "calc": calc}


def _box_rows(sp):
    boxes = list(sp.boxes or [])
    total = len(boxes)
    rows = []
    for i, b in enumerate(boxes, start=1):
        rows.append({"box": {
            "box_no": i,
            "box_no_text": f"{i}/{total}",
            "box_id": b.box_id or "",
            "msku": b.msku or "",
            "qty": b.qty,
            "length_in": b.length_in,
            "width_in": b.width_in,
            "height_in": b.height_in,
            "length_cm": _round(b.length_in * IN_TO_CM, 1) if b.length_in is not None else None,
            "width_cm": _round(b.width_in * IN_TO_CM, 1) if b.width_in is not None else None,
            "height_cm": _round(b.height_in * IN_TO_CM, 1) if b.height_in is not None else None,
            "weight_lb": b.weight_lb,
            "weight_kg": _round(b.weight_lb * LB_TO_KG, 2) if b.weight_lb is not None else None,
        }})
    return rows


def _shipment_dict(sp, item_rows, db, company):
    """货件字段 + 汇总（总毛重/净重/数量/金额）。"""
    cid = company.id if company is not None else None
    total_gross = 0.0
    total_qty = 0
    total_value = 0.0
    total_purchase = 0.0
    total_contract = 0.0
    has_contract = False
    sum_boxes = 0
    for r in item_rows:
        it = r["item"]
        bc = it["box_count"] or 0
        sum_boxes += bc
        total_qty += it["qty"] or 0
        if it["amount"] is not None:
            total_value += it["amount"]
        if it["purchase_amount"] is not None:
            total_purchase += it["purchase_amount"]
        if r["calc"].get("amount_contract") is not None:
            total_contract += r["calc"]["amount_contract"]
            has_contract = True
        # 毛重 = Σ(箱数 × 产品单箱重量)
    # 单箱重量在 Product 上，需回到 ORM 取
    for it_orm in (sp.items or []):
        p = it_orm.product
        if p is not None and p.box_weight_kg is not None:
            total_gross += (it_orm.box_count or 0) * p.box_weight_kg
    carton_num = sp.carton_num if sp.carton_num is not None else sum_boxes
    d = {
        "amazon_shipment_id": sp.amazon_shipment_id or "",
        "reference_id": sp.reference_id or "",
        "ship_sn": sp.ship_sn or "",
        "fc_code": sp.fc_code or "",
        "address_line1": sp.address_line1 or "",
        "address_line2": sp.address_line2 or "",
        "city": sp.city or "",
        "state": sp.state or "",
        "postal_code": sp.postal_code or "",
        "country_code": sp.country_code or "",
        "carton_num": carton_num,
        "total_gross_weight": _round(total_gross, 2),
        "total_net_weight": rule_engine.net_weight(total_gross, carton_num, db, cid),
        "total_volume": sp.total_volume,
        "total_qty": total_qty,
        # RPA(HUHOLE/BFPeaky 投保)把总数量按浮点写入货物描述（'512.0pcs'），
        # 复刻该口径供格式串引用
        "total_qty_float": str(float(total_qty)),
        "total_value": _round(total_value, 2),
        "total_purchase_value": _round(total_purchase, 2),
        # 投保货值/投保仓库货值口径：Σ数量×成本×合同系数，**不舍入**
        # （成品如 15618.93345）
        "total_contract_value": total_contract if has_contract else None,
        "forwarder_order_no": sp.forwarder_order_no or "",
    }
    return d


def _agg_item_rows(all_item_rows):
    """跨货件按 msku 聚合 → items_agg 行列表（采购合同=批次总量场景）。

    qty / box_count 求和，其余字段取该 msku 第一条；随数量派生的金额/毛重
    （amount、amount_vat、amount_vat_markup、insurance_amount、
    total_gross_weight）按聚合后的数量重算，否则批次总量合同金额会错。
    seq 重新连续编号。
    """
    order = []
    by_msku = {}
    for r in all_item_rows:
        k = r["item"].get("msku") or ""
        if k not in by_msku:
            by_msku[k] = {"item": dict(r["item"]), "calc": dict(r["calc"])}
            order.append(k)
        else:
            agg = by_msku[k]["item"]
            agg["qty"] = (agg["qty"] or 0) + (r["item"]["qty"] or 0)
            agg["box_count"] = (agg["box_count"] or 0) + (r["item"]["box_count"] or 0)

    rows = []
    for seq, k in enumerate(order, start=1):
        it = by_msku[k]["item"]
        calc = by_msku[k]["calc"]
        it["seq"] = seq
        qty = it["qty"] or 0
        if it["customs_unit_price"] is not None:
            it["amount"] = _round(qty * it["customs_unit_price"], 2)
        if it["purchase_cost"] is not None:
            it["purchase_amount"] = _round(qty * it["purchase_cost"], 2)
        if it["box_weight_kg"] is not None and it["box_count"] is not None:
            it["total_gross_weight"] = _round(it["box_weight_kg"] * it["box_count"], 2)
        for price_key, amount_key in (("price_vat", "amount_vat"),
                                      ("price_vat_markup", "amount_vat_markup"),
                                      ("insurance_price", "insurance_amount")):
            if calc.get(price_key) is not None:
                calc[amount_key] = _round(calc[price_key] * qty, 2)
        if calc.get("price_contract") is not None:   # 合同金额不舍入
            calc["amount_contract"] = calc["price_contract"] * qty
        rows.append(by_msku[k])
    return rows


def _const_calc(db, cid):
    """报关/投保常量（走 RuleConfig）。"""
    keys = ("origin_country", "dest_country", "unit_name", "currency_customs",
            "package_type", "supervision_mode", "tax_mode", "transport_mode",
            "delivery_mode", "dest_type", "shelf_guarantee", "departure_port",
            "fragile", "insurance_currency", "insurance_ratio")
    return {k: rule_engine.get_rule(db, k, cid) for k in keys}


def _shipment_calc(db, batch, sp, item_rows, company):
    cid = company.id if company is not None else None
    brand = batch.brand
    fc = sp.fc_code if sp is not None else ""
    calc = {
        "doc_no": rule_engine.doc_no(brand, company, fc, batch.contract_date,
                                     "shipment", db=db, country=batch.country,
                                     base_date=batch.base_date),
        "purchase_no": rule_engine.doc_no(brand, company, fc, batch.contract_date,
                                          "purchase", db=db, country=batch.country,
                                          base_date=batch.base_date),
        "contract_date_cn": _cn_date(batch.contract_date),
    }
    calc.update(_const_calc(db, cid))
    # 行级 calc 的批次/货件级兜底：取第一明细行；保价金额取合计
    if item_rows:
        first = item_rows[0]["calc"]
        calc.setdefault("price_vat", first["price_vat"])
        for k in ("price_vat", "amount_vat", "price_vat_markup",
                  "amount_vat_markup", "price_contract", "amount_contract",
                  "insurance_price"):
            calc[k] = first[k]
        ins_total = sum((r["calc"]["insurance_amount"] or 0) for r in item_rows)
        calc["insurance_amount"] = _round(ins_total, 2)
    else:
        for k in ("price_vat", "amount_vat", "price_vat_markup",
                  "amount_vat_markup", "price_contract", "amount_contract",
                  "insurance_price", "insurance_amount"):
            calc[k] = None
    return calc


# ---------------------------------------------------------------- 上下文构建

def build_context(db, batch, shipment=None):
    """组装取值上下文。shipment=None 为批次级
    （含 shipments / items_agg / plan_items 行列表数据源）。"""
    company = batch.company
    factory = batch.factory
    brand = batch.brand
    trade = None
    if db is not None:
        trade = (db.query(Company)
                 .filter(Company.type == "trade", Company.active == True)  # noqa: E712
                 .first())

    today = date.today()
    now = datetime.now()
    try:
        base = rule_engine._to_date(batch.base_date) if (batch.base_date or "").strip() \
            else rule_engine.next_friday(today)
    except (ValueError, TypeError):
        base = rule_engine.next_friday(today)
    try:
        contract_dt = (datetime.combine(rule_engine._to_date(batch.contract_date),
                                        datetime.min.time())
                       if (batch.contract_date or "").strip() else None)
    except (ValueError, TypeError):
        contract_dt = None

    ctx = {
        "batch": {
            "name": batch.name or "",
            "brand": (brand.name if brand is not None else "") or "",
            "country": batch.country or "",
            "contract_date": batch.contract_date or "",
            "contract_date_dt": contract_dt,
            "base_date": batch.base_date or "",
        },
        "company": _attr_dict(company, FIELD_DICT["company"].keys()),
        "factory": _attr_dict(factory, FIELD_DICT["factory"].keys()),
        "trade": _attr_dict(trade, FIELD_DICT["trade"].keys()),
        "meta": {
            "today": today.strftime("%Y-%m-%d"),
            "today_slash": today.strftime("%Y/%m/%d"),
            "today_mmdd": today.strftime("%m%d"),
            "today_compact": today.strftime("%Y%m%d"),
            "now_compact": now.strftime("%Y%m%d%H%M%S"),
            "base_friday": base.strftime("%Y-%m-%d"),
            "base_friday_slash": base.strftime("%Y/%m/%d"),
            "base_friday_mmdd": base.strftime("%m%d"),
            "base_friday_mm_dd": base.strftime("%m-%d"),
            "base_friday_mm_dot_dd": base.strftime("%m.%d"),
            "base_friday_dt": datetime.combine(base, datetime.min.time()),
        },
    }

    if shipment is not None:
        item_rows = [_item_row(it, i, db, company)
                     for i, it in enumerate(shipment.items or [], start=1)]
        ctx["shipment"] = _shipment_dict(shipment, item_rows, db, company)
        ctx["forwarder"] = _attr_dict(shipment.forwarder, FIELD_DICT["forwarder"].keys())
        ctx["items"] = item_rows
        ctx["boxes"] = _box_rows(shipment)
        ctx["calc"] = _shipment_calc(db, batch, shipment, item_rows, company)
        ctx["shipments"] = []
        return ctx

    # 批次级：shipments 每货件一行（货件汇总字段 + 第一明细行的产品字段 + 货件级 calc）
    # 另有两个批次级数据源：
    #   items_agg  跨货件按 msku 聚合（采购合同=批次总量）
    #   plan_items 每 (货件×SKU) 一行，calc.doc_no 用该货件的（9810 订单导入）
    ship_rows = []
    all_item_rows = []
    plan_rows = []
    fwd = None
    # 批次级行数据源按 FC 字典序排列（与历史 RPA 按文件名循环的顺序一致，
    # 也保证 9810/投保等批次级文件行序稳定可复现）
    sorted_shipments = sorted(batch.shipments or [],
                              key=lambda s: (s.fc_code or "", s.id))
    for sp in sorted_shipments:
        rows = [_item_row(it, i, db, company)
                for i, it in enumerate(sp.items or [], start=1)]
        all_item_rows.extend(rows)
        sp_dict = _shipment_dict(sp, rows, db, company)
        sp_calc = _shipment_calc(db, batch, sp, rows, company)
        sp_fwd = _attr_dict(sp.forwarder, FIELD_DICT["forwarder"].keys())
        first_item = rows[0]["item"] if rows else {k: None for k in FIELD_DICT["item"]}
        ship_rows.append({"shipment": sp_dict, "item": first_item, "calc": sp_calc,
                          "forwarder": sp_fwd})
        for r in rows:
            row_calc = dict(sp_calc)     # 货件级 doc_no/purchase_no/常量
            row_calc.update(r["calc"])   # 行级单价/金额覆盖
            plan_rows.append({"shipment": sp_dict, "item": r["item"],
                              "calc": row_calc, "forwarder": sp_fwd})
        if fwd is None and sp.forwarder is not None:
            fwd = sp.forwarder

    first_sp = sorted_shipments[0] if sorted_shipments else None
    ctx["shipment"] = ship_rows[0]["shipment"] if ship_rows else None
    ctx["forwarder"] = _attr_dict(fwd, FIELD_DICT["forwarder"].keys())
    ctx["items"] = all_item_rows
    ctx["items_agg"] = _agg_item_rows(all_item_rows)
    # items_agg_by_product：同 items_agg，但按商品主数据行序排序
    # （Product.sort_index=最近一次产品导入/当周补仓计划的行序，缺省回退
    # product_id）——HUHOLE/BFPeaky 采购合同成品的行序与当周补仓计划一致，
    # 与"首次出现序"不同；Serenorch 模板继续用 items_agg，不受影响。
    _agg = ctx["items_agg"]

    def _agg_key(i):
        it = _agg[i]["item"]
        sort = it.get("product_sort")
        pid = it.get("product_id")
        return (sort is None, sort if sort is not None else 0,
                pid is None, pid or 0, i)

    _sorted = sorted(range(len(_agg)), key=_agg_key)
    agg_by_product = []
    for new_seq, idx in enumerate(_sorted, start=1):
        row = {"item": dict(_agg[idx]["item"]), "calc": dict(_agg[idx]["calc"])}
        row["item"]["seq"] = new_seq
        agg_by_product.append(row)
    ctx["items_agg_by_product"] = agg_by_product
    ctx["plan_items"] = plan_rows
    ctx["boxes"] = _box_rows(first_sp) if first_sp is not None else []
    ctx["calc"] = _shipment_calc(db, batch, first_sp, all_item_rows, company)
    ctx["shipments"] = ship_rows
    return ctx


# ---------------------------------------------------------------- 取值

_FMT_RE = re.compile(r"\{([^{}]+)\}")


def resolve(path, ctx, row=None):
    """分级取值。

    - "text:xxx" → 字面量 "xxx"
    - "num:123" → 数值字面量 123（int 优先，含小数点转 float）
    - 含 {} → 格式串：每个 {path} 递归 resolve，None→空串
    - 其余按 "ns.key" 取值：表格行内（row 非空）时 row 里的命名空间优先，
      其余落回 ctx；数字保持数值类型。
    """
    if not isinstance(path, str):
        return path
    if path.startswith("text:"):
        return path[5:]
    if path.startswith("num:"):
        s = path[4:].strip()
        try:
            return float(s) if ("." in s or "e" in s.lower()) else int(s)
        except ValueError:
            raise ValueError(f"数值字面量 '{path}' 不是合法数字")
    if "{" in path:
        def _rep(m):
            v = resolve(m.group(1), ctx, row)
            return "" if v is None else str(v)
        return _FMT_RE.sub(_rep, path)

    parts = path.split(".")
    ns = parts[0]
    if row is not None and isinstance(row, dict) and ns in row:
        obj = row[ns]
    elif ns in ctx:
        obj = ctx[ns]
    else:
        raise ValueError(f"未知字段命名空间 '{ns}'（路径 {path}）")
    for seg in parts[1:]:
        if obj is None:
            return None
        if isinstance(obj, dict):
            if seg not in obj:
                raise ValueError(f"字段 '{path}' 未在字段字典登记（'{seg}' 不存在）")
            obj = obj[seg]
        else:
            obj = getattr(obj, seg, None)
    return obj
