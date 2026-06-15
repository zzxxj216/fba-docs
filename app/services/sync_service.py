"""赛狐同步服务：STA 入库计划 → Batch/Shipment/ShipmentItem/ShipmentBox。

同步链（全部只读赛狐）：
1. /api/inbound/getInboundPlan.json        计划 + 货件清单
2. /api/inbound/shipment/detail.json       每货件：地址/箱数/逐箱/shipSnList/shopName/marketplaceId
3. /api/fba/shippingOrder/detailByShipSn.json  明细行：SKU/数量/箱数/箱规cm/申报单价/采购成本
4. /api/fba/shippingOrder/pageList.json    兜底取 logisticProviderName（按 shipSn 匹配）

规则：
- Batch 按 inbound_plan_id 幂等；Shipment 按 sellfox_shipment_id、Item 按 msku 增量更新
- Shipment/ShipmentItem 的 edited_fields 里列出的字段更新时跳过（人工修改保护）
- 品牌按 shop_name '-' 前段忽略大小写匹配 Brand.name，带出 company/factory
- base_date=下一个周五、contract_date=上月同日（算法内联，不依赖 rule_engine）
- MSKU 不在产品库时自动建档：发货单 childItems(commodityName/commodityImage) 先填，
  再调 /api/commodity/pageList.json（body {"skus":[sku]} 精确过滤）补全报关字段；
  拉不到不阻塞，remark 标记"同步自动建档"供 validate_service 降级提示
- 同步完成自动跑 validate_service，返回 {"batch_id", "report"}
"""

import calendar
import json
import math
import os
import time
from datetime import date, datetime, timedelta

from .. import sellfox_client as sf
from ..database import BASE_DIR
from ..models import (
    Batch, Brand, Forwarder, Product, Shipment, ShipmentBox, ShipmentItem,
)
from . import validate_service

MARKETPLACE_MAP = {
    "ATVPDKIKX0DER": "US", "A2EUQ1WTGCTBG2": "CA", "A1AM78C64UM0Y8": "MX",
    "A1F83G8C2ARO7P": "UK", "A1PA6795UKMFR9": "DE", "A13V1IB3VIYZZH": "FR",
    "APJ6JRA9NG5V4": "IT", "A1RKKUPIHCS9HS": "ES", "A1805IZSGTT6HS": "NL",
    "A2NODRKZP88ZB9": "SE", "A1C3SOZRARQ6R3": "PL", "A1VC38T7YXB528": "JP",
    "A39IBJ37TRP1C6": "AU", "A2VIGQ35RCS4UG": "AE", "A21TJRUUN4KGV": "IN",
    "A19VAU5U5O7RUS": "SG",
}


# ---------------------------------------------------------------- 工具

def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _s(v):
    return "" if v is None else str(v)


def next_friday(today=None):
    """下一个周五：周一~周四→本周五；周五→当天；周六/周日→下周五。"""
    d = today or date.today()
    wd = d.weekday()                      # Mon=0 .. Sun=6
    if wd == 4:
        return d
    return d + timedelta(days=(4 - wd) % 7)


def prev_month_same_day(d):
    """上月同日；上月没有该日则取上月最后一天（31→30 等）。"""
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _set(obj, field, value, edited):
    """edited_fields 里的字段跳过（人工修改保护）。"""
    if field in edited:
        return
    setattr(obj, field, value)


AUTO_PRODUCT_REMARK = "同步自动建档，请补全报关信息"


def _auto_create_product(db, msku, raw):
    """MSKU 不在产品库时自动建档，返回 Product；建档失败返回 None（明细留空走 error）。

    数据来源（优先级从高到低）：
    1. 发货单明细行 raw（cartonLength/Width/Height/cartonQty/customsUnitPrice/purchaseCost）
       及其 childItems（commoditySku/commodityName/commodityImage）
    2. 赛狐商品库 /api/commodity/pageList.json（body {"skus":[sku]} 实测为精确过滤，
       可拉到 declareNameCh/En、hsCode、materialQuality、brandName、cartonWeight 等；
       接口失败不阻塞建档）
    """
    try:
        children = raw.get("childItems") or []
        child = (next((c for c in children if _s(c.get("commoditySku")) == msku), None)
                 or (children[0] if children else {}))
        commodity_name = _s(child.get("commodityName"))
        p = Product(
            sku=msku,
            name_customs_en=commodity_name,
            name_invoice=commodity_name,
            unit_price_default=(_f(raw.get("customsUnitPrice"))
                                or _f(child.get("customsUnitPrice"))),
            purchase_cost_default=(_f(raw.get("purchaseCost"))
                                   or _f(child.get("purchaseCost"))),
            carton_l_cm=_f(raw.get("cartonLength")),
            carton_w_cm=_f(raw.get("cartonWidth")),
            carton_h_cm=_f(raw.get("cartonHeight")),
            qty_per_box=_i(raw.get("cartonQty")),
            image_url=_s(child.get("commodityImage")),
            remark=AUTO_PRODUCT_REMARK,
        )

        # 赛狐商品库补全（拉不到不阻塞）
        lookup_sku = _s(child.get("commoditySku")) or msku
        row = None
        try:
            data = sf.call("/api/commodity/pageList.json",
                           {"pageNo": 1, "pageSize": 10, "skus": [lookup_sku]}) or {}
            row = next((r for r in (data.get("rows") or [])
                        if _s(r.get("sku")) == lookup_sku), None)
        except Exception:
            pass
        if row:
            p.name_customs_cn = _s(row.get("declareNameCh"))
            p.name_customs_en = _s(row.get("declareNameEn")) or p.name_customs_en
            p.hs_code = _s(row.get("hsCode"))
            p.material = _s(row.get("declareMaterial")) or _s(row.get("materialQuality"))
            p.usage = _s(row.get("declareUseTo")) or _s(row.get("useTo"))
            p.declare_elements = _s(row.get("declareElements"))
            p.brand_name = _s(row.get("brandName"))
            p.model = _s(row.get("declareModel")) or _s(row.get("model"))
            p.box_weight_kg = _f(row.get("cartonWeight"))          # kg/箱
            p.carton_l_cm = p.carton_l_cm or _f(row.get("cartonLength"))
            p.carton_w_cm = p.carton_w_cm or _f(row.get("cartonWidth"))
            p.carton_h_cm = p.carton_h_cm or _f(row.get("cartonHeight"))
            p.qty_per_box = p.qty_per_box or _i(row.get("cartonQty"))
            p.unit_price_default = p.unit_price_default or _f(row.get("declareCharge"))
            p.purchase_cost_default = (p.purchase_cost_default
                                       or _f(row.get("purchaseCost")))
            p.image_url = p.image_url or _s(row.get("imgUrl"))
    except Exception:
        return None
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------- 店铺名缓存
#
# 计划列表 /api/inbound/plan/page.json 只给 shopId（数字）和空的 inboundPlanName，
# 无法人工识别。shopId→店铺名 映射来源（实测 2026-06）：
#   主路径：/api/shop/pageList.json（设置/店铺授权/获取店铺列表），
#           rows[].id（字符串数字，即 shopId）+ name（如 "Serenorch-US"），
#           全账号 22 家店 1 页拿全，1 次请求。
#   降级：店铺接口失败/查不到时，取该计划 getInboundPlan 首货件的
#         shipment/detail.shopName（每页最多补 3 个未知 shopId，限流保护）。
# 结果持久化到 BASE_DIR/shop_cache.json（{shopId: 店铺名}），永久复用——
# 店铺极少变动，命中缓存零 API 消耗；刻意不建表，避免动 models.py。

SHOP_CACHE_FILE = os.path.join(BASE_DIR, "shop_cache.json")
SHOP_REFRESH_MIN_INTERVAL = 600        # 同进程内全量刷新最快 10 分钟一次
_shop_cache = {"map": None, "refreshed_at": 0.0}


def _shop_map():
    """懒加载 shop_cache.json → {shopId(str): name}；文件缺失/损坏视为空。"""
    if _shop_cache["map"] is None:
        try:
            with open(SHOP_CACHE_FILE, encoding="utf-8") as f:
                _shop_cache["map"] = {str(k): _s(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            _shop_cache["map"] = {}
    return _shop_cache["map"]


def _save_shop_map():
    try:
        with open(SHOP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_shop_cache["map"], f, ensure_ascii=False, indent=1)
    except OSError:
        pass                                         # 缓存写失败不阻塞列表


def _refresh_shops_from_api():
    """全量拉店铺授权列表写入缓存（实测 1 页拿全；防御性翻页）。"""
    m = _shop_map()
    page = 1
    while True:
        data = sf.call("/api/shop/pageList.json",
                       {"pageNo": page, "pageSize": 200}) or {}
        for r in (data.get("rows") or []):
            sid, name = _s(r.get("id")), _s(r.get("name"))
            if sid and name:
                m[sid] = name
        if page >= (_i(data.get("totalPage")) or 1):
            break
        page += 1
    _shop_cache["refreshed_at"] = time.time()
    _save_shop_map()


def _shop_name_via_shipment(inbound_plan_id):
    """降级路径：计划→首货件 shipment/detail 的 shopName（2 次请求）。"""
    plan = sf.call("/api/inbound/getInboundPlan.json",
                   {"inboundPlanId": inbound_plan_id}) or {}
    plan_info = plan.get("inboundPlan") or plan
    name = _s(plan_info.get("shopName"))
    if name:
        return name
    refs = (plan.get("shipments") or plan.get("shipmentList")
            or plan_info.get("shipments") or plan_info.get("shipmentList") or [])
    if not refs:
        return ""
    sid = refs[0].get("shipmentId") or refs[0].get("sellfoxShipmentId") or ""
    d = sf.call("/api/inbound/shipment/detail.json",
                {"inboundPlanId": inbound_plan_id, "shipmentId": sid}) or {}
    return _s(d.get("shopName"))


# ---------------------------------------------------------------- 计划列表

def list_plans(db, page=1, page_size=20):
    """赛狐 STA 入库计划分页，每行附加可读标识：

    - shop_name：shopId→店铺名（shop_cache.json 缓存，见上）
    - marketplace：marketplaceId→US/UK/DE… 标签（未知则透传原值）
    - imported/batch_id：inbound_plan_id 已在 batches 表 → 已导入
    """
    data = sf.call("/api/inbound/plan/page.json",
                   {"pageNo": page, "pageSize": page_size}) or {}
    rows = data.get("rows") or []

    # 店铺名解析：先缓存，缺的全量刷一次店铺接口（节流），仍缺的走货件降级
    shops = _shop_map()
    unknown = {_s(r.get("shopId")) for r in rows
               if _s(r.get("shopId")) and _s(r.get("shopId")) not in shops}
    if unknown and time.time() - _shop_cache["refreshed_at"] > SHOP_REFRESH_MIN_INTERVAL:
        try:
            _refresh_shops_from_api()
        except RuntimeError:
            pass
        unknown -= set(shops)
    if unknown:
        budget = 3                                   # 限流保护：每页最多补查 3 个
        dirty = False
        for r in rows:
            sid = _s(r.get("shopId"))
            if budget <= 0:
                break
            if not sid or sid in shops:
                continue
            budget -= 1
            try:
                name = _shop_name_via_shipment(_s(r.get("inboundPlanId")))
            except RuntimeError:
                name = ""
            if name:
                shops[sid] = name                    # 其余 shopId 下次打开继续补
                dirty = True
        if dirty:
            _save_shop_map()

    # 已导入标记：inbound_plan_id → batches.id
    plan_ids = [_s(r.get("inboundPlanId")) for r in rows if _s(r.get("inboundPlanId"))]
    existing = {}
    if plan_ids:
        q = (db.query(Batch.id, Batch.inbound_plan_id)
             .filter(Batch.inbound_plan_id.in_(plan_ids)))
        existing = {pid: bid for bid, pid in q}

    for r in rows:
        sid = _s(r.get("shopId"))
        mid = _s(r.get("marketplaceId"))
        pid = _s(r.get("inboundPlanId"))
        r["shop_name"] = shops.get(sid, "")
        r["marketplace"] = MARKETPLACE_MAP.get(mid, mid)
        r["imported"] = pid in existing
        r["batch_id"] = existing.get(pid)
    return data


# ---------------------------------------------------------------- 导入批次

def import_batch(db, inbound_plan_id):
    """导入/增量更新一个入库计划 → {"batch_id", "report"}。"""
    # ---------- 阶段1：拉全 API 数据（失败不留半截库） ----------
    plan = sf.call("/api/inbound/getInboundPlan.json",
                   {"inboundPlanId": inbound_plan_id}) or {}
    plan_info = plan.get("inboundPlan") or plan
    refs = (plan.get("shipments") or plan.get("shipmentList")
            or plan_info.get("shipments") or plan_info.get("shipmentList") or [])
    if not refs:
        raise RuntimeError(f"入库计划 {inbound_plan_id} 没有货件（shipments 为空）")

    details = []                                    # [(ref, detail)]
    for ref in refs:
        sid = ref.get("shipmentId") or ref.get("sellfoxShipmentId") or ""
        d = sf.call("/api/inbound/shipment/detail.json",
                    {"inboundPlanId": inbound_plan_id, "shipmentId": sid}) or {}
        details.append((ref, d))

    # 发货单详情按 shipSn 缓存（一个发货单可能覆盖多个货件）
    order_cache = {}

    def get_order(sn):
        if sn not in order_cache:
            try:
                order_cache[sn] = sf.call(
                    "/api/fba/shippingOrder/detailByShipSn.json", {"shipSn": sn}) or {}
            except RuntimeError:
                order_cache[sn] = {}
        return order_cache[sn]

    # pageList 兜底：shipSn → logisticProviderName（只拉一次，客户端过滤）
    _plmap = {}
    _plmap_loaded = [False]

    def pagelist_logistic(sn):
        if not _plmap_loaded[0]:
            _plmap_loaded[0] = True
            try:
                data = sf.call("/api/fba/shippingOrder/pageList.json",
                               {"pageNo": 1, "pageSize": 100}) or {}
                for row in (data.get("rows") or data.get("list") or []):
                    k = row.get("shipSn")
                    if k:
                        _plmap[k] = (row.get("logisticProviderName") or "")
            except RuntimeError:
                pass
        return _plmap.get(sn, "")

    # ---------- 阶段2：批次 upsert ----------
    first_detail = details[0][1] if details else {}
    shop_name = (_s(plan_info.get("shopName")) or _s(first_detail.get("shopName")))
    marketplace_id = (_s(first_detail.get("marketplaceId"))
                      or _s(plan_info.get("marketplaceId")))
    country = MARKETPLACE_MAP.get(marketplace_id, marketplace_id or "US")

    batch = db.query(Batch).filter(Batch.inbound_plan_id == inbound_plan_id).first()
    created = batch is None
    if created:
        batch = Batch(inbound_plan_id=inbound_plan_id)
        db.add(batch)

    batch.shop_name = shop_name or batch.shop_name
    batch.marketplace = country or batch.marketplace
    batch.country = country or batch.country

    # 品牌匹配：shop_name '-' 前段忽略大小写匹配 Brand.name（已绑定的不覆盖）
    brand = None
    prefix = (shop_name.split("-")[0] if shop_name else "").strip()
    if prefix:
        brand = (db.query(Brand)
                 .filter(Brand.name.ilike(prefix), Brand.active.is_(True)).first())
    if brand and not batch.brand_id:
        batch.brand_id = brand.id
        if not batch.company_id:
            batch.company_id = brand.company_id
        if not batch.factory_id:
            batch.factory_id = brand.factory_id

    # 日期规则（内联实现，不依赖 rule_engine）；已有值（可能人工改过）不覆盖
    if not batch.base_date:
        batch.base_date = next_friday().isoformat()
    if not batch.contract_date:
        base = date.fromisoformat(batch.base_date)
        batch.contract_date = prev_month_same_day(base).isoformat()

    if not batch.name:
        base = date.fromisoformat(batch.base_date)
        name_prefix = (brand.name if brand else prefix) or "批次"
        batch.name = f"{name_prefix}-{batch.country}-{base.strftime('%m%d')}"

    batch.synced_at = datetime.now()
    db.flush()                                       # 拿 batch.id

    # ---------- 阶段3：货件 upsert ----------
    incoming_sids = set()
    for ref, detail in details:
        sid = _s(ref.get("shipmentId") or detail.get("shipmentId"))
        amazon_id = _s(ref.get("amazonShipmentId") or detail.get("amazonShipmentId"))
        incoming_sids.add(sid)

        sp = (db.query(Shipment)
              .filter(Shipment.batch_id == batch.id,
                      Shipment.sellfox_shipment_id == sid).first())
        if sp is None:
            sp = Shipment(batch_id=batch.id, sellfox_shipment_id=sid)
            db.add(sp)
        edited = set(json.loads(sp.edited_fields or "[]"))

        dest_wrap = detail.get("shipmentDestination") or {}
        dest = dest_wrap.get("destination") or {}
        fc_code = (_s(dest_wrap.get("fulfillmentCenterId"))
                   or _s(detail.get("fulfillmentCenterId")))
        if not fc_code:                              # 兜底从 name 尾段解析: "FBA STA (..)-GYR2"
            nm = _s(detail.get("name"))
            if "-" in nm:
                fc_code = nm.rsplit("-", 1)[-1].strip()

        ship_sns = [s for s in (detail.get("shipSnList") or []) if s]

        _set(sp, "amazon_shipment_id", amazon_id, edited)
        _set(sp, "ship_sn", ",".join(ship_sns), edited)
        _set(sp, "reference_id", _s(detail.get("referenceId")), edited)
        _set(sp, "fc_code", fc_code, edited)
        _set(sp, "address_line1", _s(dest.get("addressLine1")), edited)
        _set(sp, "address_line2", _s(dest.get("addressLine2")), edited)
        _set(sp, "city", _s(dest.get("city")), edited)
        _set(sp, "state", _s(dest.get("stateOrProvinceCode")), edited)
        _set(sp, "postal_code", _s(dest.get("postalCode")), edited)
        _set(sp, "country_code", _s(dest.get("countryCode")) or batch.country, edited)
        _set(sp, "carton_num", _i(detail.get("cartonNum")), edited)
        _set(sp, "total_weight", _f(detail.get("weight")), edited)
        _set(sp, "total_volume", _f(detail.get("volume")), edited)
        _set(sp, "expect_arrival_start", _s(detail.get("expectArrivalStartDate")), edited)
        _set(sp, "expect_arrival_end", _s(detail.get("expectArrivalEndDate")), edited)
        db.flush()

        # 逐箱数据先行：msku → 实际箱数（明细 box_count 的第一来源；
        # 发货单 items 里的 caseNum 实测是发货单级总箱数，不能直接当每 SKU 箱数用）
        cartons = detail.get("inboundCartonVos") or []
        box_counts = {}
        for carton in cartons:
            for cit in (carton.get("itemList") or []):
                m = _s(cit.get("msku"))
                if m:
                    box_counts[m] = box_counts.get(m, 0) + 1

        # ---- 明细行：来自发货单（按 amazonShipmentId 过滤），按 msku 合并 ----
        merged = {}                                  # msku -> item dict
        logistic_name = ""
        for sn in ship_sns:
            order = get_order(sn)
            logistic_name = logistic_name or _s(order.get("logisticProviderName"))
            for it in (order.get("items") or order.get("itemList") or []):
                a = _s(it.get("amazonShipmentId"))
                if a and amazon_id and a != amazon_id:
                    continue
                msku = _s(it.get("sellerSku") or it.get("msku"))
                if not msku:
                    continue
                if msku in merged:                   # 同 SKU 多发货单 → 数量累加
                    old = merged[msku]
                    old["quantity"] = (_i(old.get("quantity")) or 0) + (_i(it.get("quantity")) or 0)
                    old["caseNum"] = (_i(old.get("caseNum")) or 0) + (_i(it.get("caseNum")) or 0)
                else:
                    merged[msku] = dict(it)
            if not logistic_name:
                logistic_name = pagelist_logistic(sn)

        # 货代匹配（找不到则按赛狐名自动建一条，便于后续配模板）
        if logistic_name:
            fw = (db.query(Forwarder)
                  .filter(Forwarder.sellfox_name == logistic_name).first()
                  or db.query(Forwarder).filter(Forwarder.name == logistic_name).first())
            if fw is None:
                fw = Forwarder(name=logistic_name, sellfox_name=logistic_name)
                db.add(fw)
                db.flush()
            _set(sp, "forwarder_id", fw.id, edited)

        existing_items = {it.msku: it for it in sp.items}
        for msku, raw in merged.items():
            item = existing_items.get(msku)
            if item is None:
                item = ShipmentItem(shipment_id=sp.id, msku=msku)
                db.add(item)
            iedited = set(json.loads(item.edited_fields or "[]"))
            product = db.query(Product).filter(Product.sku == msku).first()
            if product is None:                      # 未匹配产品库 → 自动建档
                product = _auto_create_product(db, msku, raw)
            _set(item, "product_id", product.id if product else None, iedited)
            _set(item, "fnsku", _s(raw.get("fnSku") or raw.get("fnsku")), iedited)
            _set(item, "asin", _s(raw.get("asin")), iedited)
            qty = _i(raw.get("quantity"))
            per = _i(raw.get("cartonQty"))
            bc = box_counts.get(msku)                # ① 逐箱实数
            if not bc and qty and per:
                bc = math.ceil(qty / per)            # ② qty/每箱数
            if not bc:
                bc = _i(raw.get("caseNum"))          # ③ 兜底
            _set(item, "qty", qty, iedited)
            _set(item, "box_count", bc, iedited)
            _set(item, "qty_per_box", _i(raw.get("cartonQty")), iedited)
            _set(item, "carton_l", _f(raw.get("cartonLength")), iedited)
            _set(item, "carton_w", _f(raw.get("cartonWidth")), iedited)
            _set(item, "carton_h", _f(raw.get("cartonHeight")), iedited)
            _set(item, "customs_unit_price", _f(raw.get("customsUnitPrice")), iedited)
            _set(item, "purchase_cost", _f(raw.get("purchaseCost")), iedited)
        # 源数据里已消失的明细删掉
        for msku, item in existing_items.items():
            if msku not in merged and merged:
                db.delete(item)

        # ---- 逐箱：直接重建（无人工字段） ----
        db.query(ShipmentBox).filter(ShipmentBox.shipment_id == sp.id).delete()
        for carton in cartons:
            for it in (carton.get("itemList") or []):
                db.add(ShipmentBox(
                    shipment_id=sp.id,
                    box_id=_s(carton.get("boxId")),
                    msku=_s(it.get("msku")),
                    qty=_i(it.get("quantity")),
                    length_in=_f(carton.get("length")),
                    width_in=_f(carton.get("width")),
                    height_in=_f(carton.get("height")),
                    weight_lb=_f(carton.get("weight")),
                ))

    # 计划中已不存在的货件删除
    for sp in list(batch.shipments):
        if sp.sellfox_shipment_id not in incoming_sids:
            db.delete(sp)

    db.commit()
    db.expire_all()        # 同步过程中懒加载过的集合可能已过期，校验前强制重读

    report = validate_service.validate_batch(db, batch.id)
    return {"batch_id": batch.id, "created": created, "report": report}
