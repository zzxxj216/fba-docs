"""赛狐采购计划（补货计划）服务：采购计划 ↔ STA 入库计划 配套 → 批次。

采购计划接口 /api/purchasePlan/search.json（status=1=已审批/已处理）只给"补货明细"
（SKU/计划量/箱数/采购成本/供应商），**不含发货货件拆分**（relateShipmentPlanNoDTOS=null）。
发货数据（FC/Reference ID/逐箱/箱数）只在 STA 入库计划里——所以本服务做两件事：

1. 列已审批采购计划（list_plans）
2. 为采购计划找配套 STA（candidates）、组装预览（preview）、落库（import_plan）

落库统一复用 sync_service.import_batch（它已处理品牌/货代/edited_fields/校验），
本服务只在其上叠加：①按 msku 用采购计划成本补 ShipmentItem.purchase_cost
②写 batch.purchase_plan_no。

设计取舍（见根目录 _smoke_pplan.py 报告）：
- 候选时间窗：以采购计划 createTime 为中心，[−2天, +28天]。实测目标 STA wfa98afd04
  的 createTime（2026-06-06 11:48）比采购计划 createTime（12:15）早约 27 分钟
  （先建 STA 再在采购计划里确认很常见），故不能强制"STA 晚于采购计划"，留 2 天提前量。
- 为防限流：候选先按店铺+时间窗筛选，时间倒序最多取 8 个做 getInboundPlan + 逐货件
  detail 求总量/SKU 集合。
"""

from datetime import datetime, timedelta

from .. import sellfox_client as sf
from ..models import Batch, Brand, Product, Shipment, ShipmentItem
from . import sync_service as ss
from .sync_service import _f, _i, _s, MARKETPLACE_MAP

PURCHASE_PLAN_PATH = "/api/purchasePlan/search.json"
INBOUND_PAGE_PATH = "/api/inbound/plan/page.json"

# 候选时间窗（相对采购计划 createTime）
CAND_LEAD_DAYS = 2          # STA 可比采购计划早最多 2 天
CAND_TRAIL_DAYS = 28        # STA 可比采购计划晚最多 4 周
CAND_MAX_PROBE = 8          # 最多对 8 个候选做 getInboundPlan + detail（限流保护）
CAND_SCAN_PAGES = 4         # 扫 STA 列表最多 4 页找同店铺候选
DEFAULT_RANGE_DAYS = 60     # 采购计划默认拉近 60 天


# ---------------------------------------------------------------- 时间兼容

def _parse_time(v):
    """采购计划/STA createTime 兼容：毫秒时间戳(int) 或 'yyyy-MM-dd HH:mm:ss' 字符串。"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s) / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_time(v):
    """统一成可读字符串（前端展示用）。"""
    dt = _parse_time(v)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else _s(v)


# ---------------------------------------------------------------- 采购计划明细

def _item_view(it):
    """采购计划明细行 → 契约形状。"""
    return {
        "sku": _s(it.get("sku")),
        "msku": _s(it.get("msku")),
        "commodity_name": _s(it.get("commodityName")),
        "main_image": _s(it.get("mainImage")),
        "shop_name": _s(it.get("shopName")),
        "site_name": _s(it.get("siteName")),
        "plan_num": _i(it.get("planNum")),
        "carton_num": _i(it.get("cartonNum")),
        "carton_qty": _i(it.get("cartonQty")),
        "purchase_cost": _f(it.get("purchaseCost")),
        "supplier_name": _s(it.get("supplierName")),
        "brand_name": _s(it.get("brandName")),
    }


def _plan_status(grp):
    """从首明细行推断采购计划状态 → (status_code, 中文标签)。

    赛狐采购计划明细 status 枚举（2026-06-15 实测+用户确认）：
      0=待采购(审核通过未采购) / 1=已处理(已采购) / 3=待审核 / 4=审核驳回。
    已采购优先用 purchaseStatus==2 或有采购单号判定（最可靠，见 PURCHASE_ORDER_API）。
    """
    its = grp.get("purchasePlanItemVoList") or []
    it = its[0] if its else {}
    st = it.get("status")
    review = it.get("reviewTime")
    pstatus = it.get("purchaseStatus")
    po = it.get("purchaseNo")
    if pstatus == 2 or st == 1 or po:
        return st, "已采购"
    if st == 4:
        return st, "已驳回"
    if st == 3 or not review:
        return st, "待审核"
    return st, "待采购"


def _group_view(db, grp):
    """采购计划组 → 契约形状（含 items + imported/batch_id + 状态）。"""
    items = [_item_view(it) for it in (grp.get("purchasePlanItemVoList") or [])]
    pgn = _s(grp.get("planGroupNo"))
    st_code, st_label = _plan_status(grp)
    batch = (db.query(Batch.id).filter(Batch.purchase_plan_no == pgn).first()
             if pgn else None)
    # itemCount 列接口常返 0，用实际明细行数兜底
    item_count = _i(grp.get("itemCount")) or len(items)
    return {
        "plan_group_no": pgn,
        "create_name": _s(grp.get("createName")),
        "create_time": _fmt_time(grp.get("createTime")),
        "plan_total_num": _i(grp.get("planTotalNum")),
        "item_count": item_count,
        "warehouse_name": _s(grp.get("warehouseName")),
        "imported": batch is not None,
        "batch_id": batch[0] if batch else None,
        "status": st_code,
        "status_label": st_label,
        # 是否已建仓发货（首明细有店铺=已分配FBA仓，能配套发货数据走完整流程）
        "has_shipping": bool(items and items[0].get("shop_name")),
        "items": items,
    }


def _search_plans(page=1, page_size=20, start=None, end=None):
    """调采购计划接口（不带 status，拉全部状态：待审核/待采购/已采购）。
    返回 (rows, total_size)。注：赛狐 status 入参只接受 0/1，不传则返回全部。"""
    end = end or datetime.now()
    start = start or (end - timedelta(days=DEFAULT_RANGE_DAYS))
    data = sf.call(PURCHASE_PLAN_PATH, {
        "pageNo": page, "pageSize": page_size,
        "createTimeStart": start.strftime("%Y-%m-%d"),
        "createTimeEnd": end.strftime("%Y-%m-%d"),
    }) or {}
    return (data.get("rows") or []), _i(data.get("totalSize")) or 0


_FETCH_MAX_ROWS = 500   # 翻页拉全上限（每页 1 次赛狐调用，限流 1.1s/次）


def _fetch_all_rows(days=None):
    """按时间窗翻页拉全采购计划（每页 100，最多 _FETCH_MAX_ROWS 条）。
    返回 (rows, 赛狐侧总条数)——rows 少于总条数即被上限截断，调用方要透出。"""
    end = datetime.now()
    start = end - timedelta(days=days or DEFAULT_RANGE_DAYS)
    rows, total = _search_plans(page=1, page_size=100, start=start, end=end)
    page = 1
    while rows and len(rows) < min(total, _FETCH_MAX_ROWS):
        page += 1
        more, _ = _search_plans(page=page, page_size=100, start=start, end=end)
        if not more:
            break
        rows.extend(more)
    return rows, total


def _resolve_shop_key(brands, shop):
    """shop 入参 → 匹配用品牌名。接受品牌缩写(ZE/RA/BY)、品牌名(Zentop)；
    解析不到 Brand 时原样返回，走店铺名包含匹配。"""
    w = _s(shop).strip()
    if not w:
        return ""
    for b in brands:
        if (b.abbr2 or "").upper() == w.upper() or (b.name or "").lower() == w.lower():
            return b.name
    return w


def _group_matches(g, brand_name, site, brand_sites):
    """组内任一明细同时命中店铺+站点即算命中（不能只看首明细，混合计划会漏）。
    店铺按 shopName '-' 前段或 brandName 匹配（shopName 形如 Zentop-US；
    未建仓的明细 shopName 可能为空，靠 brandName 兜底）；
    site 接受中文站名(美国)或国家码(US)——明细无 siteName（未关联店铺的计划，
    ZE/RA/BY 常态）时用品牌 default_site 兜底参与匹配。"""
    want_site = _s(site).strip()

    def shop_ok(it):
        if not brand_name:
            return True
        w = brand_name.lower()
        sn = _s(it.get("shop_name"))
        head = sn.split("-")[0].strip().lower() if sn else ""
        return head == w or _s(it.get("brand_name")).lower() == w or (
            bool(sn) and w in sn.lower())

    def site_ok(it):
        if not want_site:
            return True
        sname = _s(it.get("site_name"))
        if not sname:
            key = (_s(it.get("brand_name")) or
                   _s(it.get("shop_name")).split("-")[0].strip()).lower()
            sname = brand_sites.get(key, "")
        return sname == want_site or _COUNTRY.get(sname, "").upper() == want_site.upper()

    return any(shop_ok(it) and site_ok(it) for it in (g.get("items") or []))


def list_plans(db, page=1, page_size=20, status=None, shop=None, site=None, days=None):
    """列采购计划。status 按 status_label 过滤（待审核/待采购/已采购/已驳回）；
    shop=品牌缩写(ZE/RA/BY)/品牌名/店铺名，site=中文站名或国家码，days 覆盖默认
    60 天时间窗。赛狐 search.json 不支持店铺/站点入参，故翻页拉全后内存过滤；
    status_counts 统计的是 shop/site 过滤后的全集（按店铺看各状态数量）。"""
    rows, sellfox_total = _fetch_all_rows(days=days)
    groups = [_group_view(db, r) for r in rows]
    brands = db.query(Brand).all()
    brand_name = _resolve_shop_key(brands, shop)
    if brand_name or _s(site).strip():
        brand_sites = {(b.name or "").lower(): _s(b.default_site) for b in brands}
        groups = [g for g in groups if _group_matches(g, brand_name, site, brand_sites)]
    counts = {"全部": len(groups)}
    for lbl in ("待审核", "待采购", "已采购", "已驳回"):
        counts[lbl] = sum(1 for g in groups if g.get("status_label") == lbl)
    if status and status not in ("", "全部"):
        groups = [g for g in groups if g.get("status_label") == status]
    s = (page - 1) * page_size
    return {
        "plans": groups[s:s + page_size],
        "page": page,
        "total_size": len(groups),
        "status_counts": counts,
        # 赛狐侧原始总条数；大于已拉取数说明触到 _FETCH_MAX_ROWS 截断，需缩小时间窗
        "sellfox_total": sellfox_total,
        "fetched": len(rows),
    }


def _find_group(db, plan_group_no):
    """按 plan_group_no 取单个采购计划组（翻页查找，近 60 天）。找不到返回 None。"""
    page = 1
    while True:
        rows, total = _search_plans(page=page, page_size=50)
        for r in rows:
            if _s(r.get("planGroupNo")) == plan_group_no:
                return r
        if page * 50 >= total or not rows:
            return None
        page += 1


# ---------------------------------------------------------------- STA 候选

def _shop_name_for_sta_row(row, shops):
    return shops.get(_s(row.get("shopId")), "")


def _scan_sta_for_shop(shop_name, plan_time):
    """扫 STA 列表，筛出同店铺、时间窗内的入库计划（时间倒序，最多 CAND_MAX_PROBE 个）。

    返回 [(inbound_plan_id, create_dt_str)]。
    """
    shops = ss._shop_map()
    # 若店铺缓存里没有任何映射含该店铺名，刷一次（防御）
    if shop_name and shop_name not in shops.values():
        try:
            ss._refresh_shops_from_api()
        except RuntimeError:
            pass
        shops = ss._shop_map()

    lo = plan_time - timedelta(days=CAND_LEAD_DAYS) if plan_time else None
    hi = plan_time + timedelta(days=CAND_TRAIL_DAYS) if plan_time else None

    found = []
    page = 1
    while page <= CAND_SCAN_PAGES:
        data = sf.call(INBOUND_PAGE_PATH, {"pageNo": page, "pageSize": 50}) or {}
        rows = data.get("rows") or []
        if not rows:
            break
        for r in rows:
            if _shop_name_for_sta_row(r, shops) != shop_name:
                continue
            ct = _parse_time(r.get("createTime"))
            if plan_time and ct:
                if ct < lo or ct > hi:
                    continue
            found.append({
                "inbound_plan_id": _s(r.get("inboundPlanId")),
                "create_time": r.get("createTime"),
                "create_dt": ct,
                "marketplace": MARKETPLACE_MAP.get(_s(r.get("marketplaceId")),
                                                   _s(r.get("marketplaceId"))),
            })
        total_page = _i(data.get("totalPage")) or 1
        if page >= total_page:
            break
        page += 1

    # 时间倒序，取前 N 个做明细探测
    found.sort(key=lambda x: (x["create_dt"] or datetime.min), reverse=True)
    return found[:CAND_MAX_PROBE]


def _fetch_sta_summary(inbound_plan_id):
    """对一个 STA 拉总量/SKU 集合/FC/货件数（getInboundPlan + 各 shipment/detail）。

    返回 {shipment_count, total_qty, fc_list, skus(set), shop_name, marketplace,
          create_time, name}。
    """
    plan = sf.call("/api/inbound/getInboundPlan.json",
                   {"inboundPlanId": inbound_plan_id}) or {}
    plan_info = plan.get("inboundPlan") or plan
    refs = (plan.get("shipments") or plan.get("shipmentList")
            or plan_info.get("shipments") or plan_info.get("shipmentList") or [])
    total_qty = 0
    fc_list = []
    skus = set()
    shop_name = _s(plan_info.get("shopName"))
    marketplace = MARKETPLACE_MAP.get(_s(plan_info.get("marketplaceId")),
                                      _s(plan_info.get("marketplaceId")))
    for ref in refs:
        sid = ref.get("shipmentId") or ref.get("sellfoxShipmentId") or ""
        d = sf.call("/api/inbound/shipment/detail.json",
                    {"inboundPlanId": inbound_plan_id, "shipmentId": sid}) or {}
        # 货件总量：detail.quantity 直接给（实测各货件 SKU 数量汇总）
        q = _i(d.get("quantity"))
        if q is None:                                # 兜底：逐箱求和
            q = sum(_i(cit.get("quantity")) or 0
                    for c in (d.get("inboundCartonVos") or [])
                    for cit in (c.get("itemList") or []))
        total_qty += q or 0
        fc = (d.get("shipmentDestination") or {}).get("fulfillmentCenterId") or ""
        if fc:
            fc_list.append(fc)
        for it in (d.get("itemList") or []):
            m = _s(it.get("msku"))
            if m:
                skus.add(m)
        if not shop_name:
            shop_name = _s(d.get("shopName"))
        if not marketplace:
            marketplace = MARKETPLACE_MAP.get(_s(d.get("marketplaceId")),
                                              _s(d.get("marketplaceId")))
    return {
        "shipment_count": len(refs),
        "total_qty": total_qty,
        "fc_list": fc_list,
        "skus": skus,
        "shop_name": shop_name,
        "marketplace": marketplace,
        "create_time": _fmt_time(plan_info.get("createTime")),
    }


def _score(plan_total, plan_skus, sta_total, sta_skus):
    """匹配打分：总量一致+SKU一致=100；总量一致 SKU 不全一致=80；仅店铺一致=50。"""
    qty_match = (plan_total is not None and sta_total == plan_total)
    sku_match = bool(plan_skus) and (set(sta_skus) == set(plan_skus))
    if qty_match and sku_match:
        return 100, "总量与SKU完全一致"
    if qty_match:
        return 80, "总量一致，SKU集合不完全一致"
    return 50, "同店铺时间窗内候选（总量/SKU不一致）"


def candidates(db, plan_group_no):
    """为采购计划找配套 STA 入库计划。"""
    grp = _find_group(db, plan_group_no)
    if grp is None:
        return None
    items = grp.get("purchasePlanItemVoList") or []
    plan_total = _i(grp.get("planTotalNum"))
    plan_skus = {_s(it.get("msku")) for it in items if _s(it.get("msku"))}
    shop_name = next((_s(it.get("shopName")) for it in items if it.get("shopName")), "")
    plan_time = _parse_time(grp.get("createTime"))

    cand_rows = _scan_sta_for_shop(shop_name, plan_time) if shop_name else []

    # 已导入标记（STA 在 batches 表）
    sta_ids = [c["inbound_plan_id"] for c in cand_rows]
    imported_map = {}
    if sta_ids:
        q = (db.query(Batch.id, Batch.inbound_plan_id)
             .filter(Batch.inbound_plan_id.in_(sta_ids)))
        imported_map = {pid: bid for bid, pid in q}

    out = []
    auto_id = None
    auto_count = 0
    for c in cand_rows:
        summ = _fetch_sta_summary(c["inbound_plan_id"])
        score, reason = _score(plan_total, plan_skus, summ["total_qty"], summ["skus"])
        if score == 100:
            auto_count += 1
            auto_id = c["inbound_plan_id"]
        bid = imported_map.get(c["inbound_plan_id"])
        out.append({
            "inbound_plan_id": c["inbound_plan_id"],
            "name": shop_name or summ["shop_name"],
            "shop_name": summ["shop_name"] or shop_name,
            "marketplace": summ["marketplace"] or c["marketplace"],
            "create_time": summ["create_time"] or _fmt_time(c["create_time"]),
            "shipment_count": summ["shipment_count"],
            "total_qty": summ["total_qty"],
            "fc_list": summ["fc_list"],
            "match_score": score,
            "match_reason": reason,
            "imported": bid is not None,
            "batch_id": bid,
        })

    out.sort(key=lambda x: x["match_score"], reverse=True)
    # 唯一 100 分才自动匹配；多个 100 分 = 不自动（交由手选）
    if auto_count != 1:
        auto_id = None
    return {"auto_inbound_plan_id": auto_id, "candidates": out}


# ---------------------------------------------------------------- 预览

def _fetch_shipment_plan(inbound_plan_id):
    """组装 STA 发货计划预览（getInboundPlan + 各 shipment/detail）。"""
    plan = sf.call("/api/inbound/getInboundPlan.json",
                   {"inboundPlanId": inbound_plan_id}) or {}
    plan_info = plan.get("inboundPlan") or plan
    refs = (plan.get("shipments") or plan.get("shipmentList")
            or plan_info.get("shipments") or plan_info.get("shipmentList") or [])
    shipments = []
    tot_carton = 0
    tot_qty = 0
    for ref in refs:
        sid = ref.get("shipmentId") or ref.get("sellfoxShipmentId") or ""
        d = sf.call("/api/inbound/shipment/detail.json",
                    {"inboundPlanId": inbound_plan_id, "shipmentId": sid}) or {}
        dest_wrap = d.get("shipmentDestination") or {}
        dest = dest_wrap.get("destination") or {}
        fc = (_s(dest_wrap.get("fulfillmentCenterId"))
              or _s(d.get("fulfillmentCenterId")))
        addr = ", ".join(p for p in (
            _s(dest.get("addressLine1")), _s(dest.get("city")),
            _s(dest.get("stateOrProvinceCode")), _s(dest.get("postalCode")),
            _s(dest.get("countryCode"))) if p)
        # 逐 SKU：itemList（msku/quantity），box_count 由逐箱求得
        cartons = d.get("inboundCartonVos") or []
        box_counts = {}
        for c in cartons:
            for cit in (c.get("itemList") or []):
                m = _s(cit.get("msku"))
                if m:
                    box_counts[m] = box_counts.get(m, 0) + 1
        items = []
        sh_qty = 0
        for it in (d.get("itemList") or []):
            m = _s(it.get("msku"))
            q = _i(it.get("quantity")) or 0
            sh_qty += q
            bc = box_counts.get(m)
            items.append({
                "msku": m,
                "qty": q,
                "box_count": bc,
                "qty_per_box": (q // bc if bc else None),
            })
        carton_num = _i(d.get("cartonNum")) or len(cartons)
        ship_qty = _i(d.get("quantity"))
        if ship_qty is None:
            ship_qty = sh_qty
        tot_carton += carton_num or 0
        tot_qty += ship_qty or 0
        shipments.append({
            "amazon_shipment_id": _s(d.get("amazonShipmentId")
                                     or ref.get("amazonShipmentId")),
            "fc_code": fc,
            "reference_id": _s(d.get("referenceId")),
            "carton_num": carton_num,
            "total_qty": ship_qty,
            "address": addr,
            "items": items,
        })
    return {
        "shipments": shipments,
        "totals": {
            "carton_num": tot_carton,
            "qty": tot_qty,
            "shipment_count": len(shipments),
        },
    }


def preview(db, plan_group_no, inbound_plan_id):
    """组装完整预览（不落库）：restock + shipment_plan + consistency。"""
    grp = _find_group(db, plan_group_no)
    if grp is None:
        return None
    items = grp.get("purchasePlanItemVoList") or []
    restock_items = []
    purchase_total = 0
    purchase_skus = {}
    for it in items:
        m = _s(it.get("msku"))
        pn = _i(it.get("planNum")) or 0
        purchase_total += pn
        if m:
            purchase_skus[m] = purchase_skus.get(m, 0) + pn
        restock_items.append({
            "sku": _s(it.get("sku")),
            "msku": m,
            "commodity_name": _s(it.get("commodityName")),
            "main_image": _s(it.get("mainImage")),
            "shop_name": _s(it.get("shopName")),
            "site_name": _s(it.get("siteName")),
            "plan_num": _i(it.get("planNum")),
            "carton_qty": _i(it.get("cartonQty")),
            "carton_num": _i(it.get("cartonNum")),
            "purchase_cost": _f(it.get("purchaseCost")),
            "supplier_name": _s(it.get("supplierName")),
        })

    shipment_plan = _fetch_shipment_plan(inbound_plan_id)
    shipment_total = shipment_plan["totals"]["qty"]

    # SKU 差异比对
    ship_skus = {}
    for sp in shipment_plan["shipments"]:
        for it in sp["items"]:
            m = it["msku"]
            if m:
                ship_skus[m] = ship_skus.get(m, 0) + (it["qty"] or 0)
    sku_diff = []
    for m in sorted(set(purchase_skus) | set(ship_skus)):
        pq = purchase_skus.get(m)
        sq = ship_skus.get(m)
        if pq != sq:
            sku_diff.append({"msku": m, "purchase_qty": pq, "shipment_qty": sq})

    return {
        "restock": {"total_num": purchase_total, "items": restock_items},
        "shipment_plan": shipment_plan,
        "consistency": {
            "purchase_total": purchase_total,
            "shipment_total": shipment_total,
            "matched": (purchase_total == shipment_total and not sku_diff),
            "sku_diff": sku_diff,
        },
    }


# ---------------------------------------------------------------- 导入

def import_plan(db, plan_group_no, inbound_plan_id):
    """导入成批次：复用 sync_service.import_batch，再叠加采购成本回填 + 记采购计划号。"""
    grp = _find_group(db, plan_group_no)
    if grp is None:
        return None

    # 采购计划成本表：msku -> purchaseCost
    cost_map = {}
    for it in (grp.get("purchasePlanItemVoList") or []):
        m = _s(it.get("msku"))
        c = _f(it.get("purchaseCost"))
        if m and c is not None:
            cost_map[m] = c

    # ① 复用 import_batch 拉 STA 落库（含品牌/货代/edited_fields/校验，内部已 commit）
    result = ss.import_batch(db, inbound_plan_id)
    batch_id = result["batch_id"]
    batch = db.get(Batch, batch_id)

    # ② 采购成本回填：仅当 ShipmentItem.purchase_cost 为空且未被 edited_fields 保护时覆盖
    import json
    updated = 0
    for sp in batch.shipments:
        for item in sp.items:
            if not cost_map:
                break
            c = cost_map.get(_s(item.msku))
            if c is None:
                continue
            edited = set(json.loads(item.edited_fields or "[]"))
            if "purchase_cost" in edited:
                continue
            if item.purchase_cost in (None, 0):
                item.purchase_cost = c
                updated += 1

    # ③ 记采购计划组号
    batch.purchase_plan_no = plan_group_no
    db.commit()

    result["purchase_plan_no"] = plan_group_no
    result["purchase_cost_filled"] = updated
    return result


_COUNTRY = {
    "美国": "US", "英国": "UK", "加拿大": "CA", "德国": "DE", "日本": "JP",
    "法国": "FR", "意大利": "IT", "西班牙": "ES", "荷兰": "NL", "瑞典": "SE",
    "波兰": "PL", "比利时": "BE", "爱尔兰": "IE", "墨西哥": "MX",
    "澳大利亚": "AU", "新加坡": "SG", "阿联酋": "AE", "巴西": "BR",
}


def import_plan_only(db, plan_group_no):
    """不配对 STA，直接从采购计划建批次。

    用于"还没建仓、STA 不存在"的情形：把采购计划明细(SKU/数量/采购成本/箱规)
    落到一个 fc_code 为空的"未分仓"货件；目的仓 FC/分仓 等后续建仓产出再补。
    已存在(按 purchase_plan_no)则只更新批次基本信息，不动已有货件(避免覆盖建仓产出)。
    """
    grp = _find_group(db, plan_group_no)
    if grp is None:
        return None
    items = grp.get("purchasePlanItemVoList") or []
    if not items:
        raise RuntimeError("采购计划无明细")

    brand_name = next((_s(it.get("brandName")) for it in items if it.get("brandName")), "")
    shop_name = next((_s(it.get("shopName")) for it in items if it.get("shopName")), "")
    site = next((_s(it.get("siteName")) for it in items if it.get("siteName")), "")
    country = _COUNTRY.get(site, site or "US")
    brand = db.query(Brand).filter(Brand.name == brand_name).first() if brand_name else None

    batch = db.query(Batch).filter(Batch.purchase_plan_no == plan_group_no).first()
    created = batch is None
    if batch is None:
        batch = Batch(purchase_plan_no=plan_group_no)
        db.add(batch)
    batch.name = f"{shop_name or brand_name or plan_group_no}-{country}"
    if brand:
        batch.brand_id = brand.id
        batch.company_id = brand.company_id
        batch.factory_id = brand.factory_id
    batch.shop_name = shop_name
    batch.marketplace = country
    batch.country = country
    if created:
        batch.status = "数据准备"
    batch.synced_at = datetime.now()
    db.flush()

    if created:
        sp = Shipment(batch_id=batch.id, fc_code="", status="待建仓")
        db.add(sp)
        db.flush()
        for it in items:
            msku = _s(it.get("msku") or it.get("sku"))
            if not msku:
                continue
            p = db.query(Product).filter(Product.sku == msku).first()
            if p is None:                       # 未匹配产品库 → 自动建档（赛狐商品接口补全）
                raw = {"childItems": [{"commoditySku": msku,
                                       "commodityName": _s(it.get("commodityName")),
                                       "commodityImage": _s(it.get("mainImage"))}],
                       "cartonQty": _i(it.get("cartonQty")),
                       "purchaseCost": _f(it.get("purchaseCost"))}
                p = ss._auto_create_product(db, msku, raw)
            db.add(ShipmentItem(
                shipment_id=sp.id, product_id=(p.id if p else None), msku=msku,
                qty=_i(it.get("planNum")) or 0,
                qty_per_box=_i(it.get("cartonQty")),
                box_count=_i(it.get("cartonNum")),
                purchase_cost=_f(it.get("purchaseCost")),
                customs_unit_price=(p.unit_price_default if p else None),
            ))
    db.commit()
    return {"batch_id": batch.id, "created": created,
            "purchase_plan_no": plan_group_no, "shipment_count": len(batch.shipments)}
