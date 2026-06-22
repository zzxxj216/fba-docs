"""工厂确认 → 生成赛狐采购单 服务。

业务流程（见 PURCHASE_ORDER_API.md / CLAUDE.md §业务背景）：
  采购计划[待审核] → 人工工厂确认(核对产品/数量/箱数) → [已确认]
                  → 生成采购单(调赛狐 /api/purchase/create.json，按品牌带供应商)。

本表 PurchasePlanConfirm 只存本地工作流状态（确认快照 + 生成的采购单号），
采购计划本身实时拉赛狐（purchase_plan_service）。与赛狐采购员两边并行。

组装规则（PURCHASE_ORDER_API.md）：
- 供应商id：取明细第一行 supplierId（赛狐已带）；为空则按 brandName 查 Brand.sellfox_supplier_id
- 仓库id：按 brandName 查 Brand.sellfox_warehouse_id；为空则用默认仓库 56115
- includeTax 固定 "0"（采购成本不含税）
- items[]：每个采购计划明细 → sku/commodityId/num(planNum)/perPurchase(purchaseCost)/
           purchasePlanNo(plan_group_no)/expectArrivalTime
"""

import json
import re
from datetime import datetime, timedelta

from .. import sellfox_client as sf
from ..models import Brand, PurchasePlanConfirm
from . import purchase_plan_service as pps
from .sync_service import _f, _i, _s

CREATE_PO_PATH = "/api/purchase/create.json"
ORDER_PO_PATH = "/api/purchase/order.json"      # 下单：待下单→待到货
ARRIVAL_PO_PATH = "/api/purchase/arrival.json"  # 到货：待到货→已完成
CANCEL_PO_PATH = "/api/purchase/cancel.json"    # 作废：清空回退到待采购
PAGE_PO_PATH = "/api/purchase/page.json"        # 采购单实时状态
DEFAULT_WAREHOUSE_ID = "56115"   # 采购计划「默认仓库」对应赛狐仓库id

# 本系统采购流程状态链（PurchasePlanConfirm.status）：
#   待审核(采购计划) →[工厂确认]→ 待采购 →[生成采购单]→ 待下单 →[下单]→ 待到货 →[到货]→ 已完成
ST_CONFIRMED = "待采购"     # 工厂确认后
ST_ORDERED = "待下单"       # 生成采购单后
ST_SHIPPING = "待到货"      # 下单后
ST_DONE = "已完成"          # 到货后

# 赛狐采购单 status → 中文（page.json data.rows[].status）
PO_STATUS_LABEL = {
    -3: "草稿", -1: "待审核", 0: "待下单", 1: "待到货", 2: "已完成", 3: "已取消",
}


def _brand(db, brand_name):
    """按品牌名查 Brand（取首个匹配）。找不到返回 None。"""
    if not brand_name:
        return None
    return (db.query(Brand)
            .filter(Brand.name == brand_name)
            .first())


def _first_brand_name(items):
    """采购计划明细里第一行非空 brandName。"""
    for it in items:
        bn = _s(it.get("brandName"))
        if bn:
            return bn
    return ""


def _num_str(v):
    """数量/单价转字符串；None → ''。整数去掉小数点。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _arrival_date(v, default_days=7):
    """预计到货时间 → yyyy-MM-dd（赛狐要求）。空/无效 → 今天+default_days 天。

    兼容 'yyyy-MM-dd HH:mm:ss'（取前10位）、毫秒/秒时间戳。
    """
    s = _s(v)
    if s:
        if len(s) >= 10 and s[:4].isdigit() and s[4] == "-":
            return s[:10]
        if s.isdigit():
            ts = int(s)
            ts = ts / 1000 if ts > 1e11 else ts
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass
    return (datetime.now() + timedelta(days=default_days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 确认记录

def _confirm_dict(rec):
    """PurchasePlanConfirm → 契约 dict。"""
    snapshot = []
    if rec.items_snapshot:
        try:
            snapshot = json.loads(rec.items_snapshot)
        except (ValueError, TypeError):
            snapshot = []
    return {
        "plan_group_no": rec.plan_group_no,
        "status": rec.status,
        "brand_name": rec.brand_name,
        "supplier_id": rec.supplier_id,
        "warehouse_id": rec.warehouse_id,
        "purchase_no": rec.purchase_no,
        "po_action": rec.po_action,
        "confirmed_at": rec.confirmed_at.strftime("%Y-%m-%d %H:%M:%S") if rec.confirmed_at else "",
        "confirmed_by": rec.confirmed_by,
        "items_snapshot": snapshot,
        "po_created_at": rec.po_created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.po_created_at else "",
        "remark": rec.remark or "",
        "created_at": rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
    }


def confirm_plan(db, plan_group_no, items, confirmed_by="", remark=""):
    """人工工厂确认：按 plan_group_no upsert PurchasePlanConfirm。

    items = 前端核对后的明细快照（如 [{sku,commodity_name,num,box_count}]）。
    品牌名从采购计划明细 brandName 取（确保和赛狐一致）。
    """
    grp = pps._find_group(db, plan_group_no)
    brand_name = ""
    if grp is not None:
        brand_name = _first_brand_name(grp.get("purchasePlanItemVoList") or [])

    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no)
           .first())
    if rec is None:
        rec = PurchasePlanConfirm(plan_group_no=plan_group_no)
        db.add(rec)

    rec.status = ST_CONFIRMED
    rec.brand_name = brand_name
    rec.items_snapshot = json.dumps(items or [], ensure_ascii=False)
    rec.confirmed_by = confirmed_by or ""
    rec.confirmed_at = datetime.now()
    if remark:
        rec.remark = remark
    db.commit()
    db.refresh(rec)
    return _confirm_dict(rec)


def _query_po_status(purchase_no):
    """查赛狐采购单实时 status（page.json）→ (status_int, label)。查不到 → (None, '')。

    purchase_no 可能逗号分隔多个，取第一个查询。任何异常都吞掉（仅用于展示）。
    """
    no = (purchase_no or "").split(",")[0].strip()
    if not no:
        return None, ""
    try:
        resp = sf.call(PAGE_PO_PATH, {"purchaseNos": [no], "pageNo": 1, "pageSize": 1})
    except Exception:
        return None, ""
    rows = []
    if isinstance(resp, dict):
        rows = resp.get("rows") or resp.get("list") or resp.get("records") or []
        if not rows and isinstance(resp.get("data"), dict):
            d = resp["data"]
            rows = d.get("rows") or d.get("list") or d.get("records") or []
    if not rows:
        return None, ""
    st = rows[0].get("status")
    try:
        st = int(st)
    except (TypeError, ValueError):
        return None, ""
    return st, PO_STATUS_LABEL.get(st, "")


def get_confirm(db, plan_group_no, with_po_status=True):
    """返回该计划的确认记录。无记录 → {plan_group_no, status:'待审核'}。

    有采购单号时（with_po_status）顺带查赛狐采购单实时状态，附 po_status/po_status_label。
    """
    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no)
           .first())
    if rec is None:
        return {"plan_group_no": plan_group_no, "status": "待审核"}
    out = _confirm_dict(rec)
    if with_po_status and rec.purchase_no:
        st, label = _query_po_status(rec.purchase_no)
        if label:
            out["po_status"] = st
            out["po_status_label"] = label
    return out


def delete_confirm(db, plan_group_no):
    """删除本地采购确认记录（只删本系统工作流记录，不碰赛狐采购单）。

    返回 {deleted, plan_group_no, purchase_no?, status?}。无记录 deleted=0。
    """
    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no).first())
    if rec is None:
        return {"deleted": 0, "plan_group_no": plan_group_no}
    info = {"plan_group_no": plan_group_no, "purchase_no": rec.purchase_no or "", "status": rec.status}
    db.delete(rec)
    db.commit()
    return {"deleted": 1, **info}


# ---------------------------------------------------------------- 采购单组装

def build_po_body(db, plan_group_no, action="0"):
    """组装赛狐采购单请求体（不调用赛狐），用于预览核对。

    返回 {body, supplier_name, warehouse_name, brand_name, item_count, total_num}。
    找不到采购计划返回 None。
    """
    grp = pps._find_group(db, plan_group_no)
    if grp is None:
        return None
    plan_items = grp.get("purchasePlanItemVoList") or []
    brand_name = _first_brand_name(plan_items)
    brand = _brand(db, brand_name)

    # 供应商id：明细首行 supplierId 优先，空则品牌兜底
    supplier_id = ""
    for it in plan_items:
        sid = _s(it.get("supplierId"))
        if sid:
            supplier_id = sid
            break
    if not supplier_id and brand is not None:
        supplier_id = _s(brand.sellfox_supplier_id)

    supplier_name = ""
    for it in plan_items:
        sn = _s(it.get("supplierName"))
        if sn:
            supplier_name = sn
            break

    # 仓库id：品牌仓 → 默认仓库
    warehouse_id = ""
    if brand is not None:
        warehouse_id = _s(brand.sellfox_warehouse_id)
    if not warehouse_id:
        warehouse_id = DEFAULT_WAREHOUSE_ID
    warehouse_name = _s(grp.get("warehouseName"))
    if not warehouse_name:
        for it in plan_items:
            wn = _s(it.get("warehouseName"))
            if wn:
                warehouse_name = wn
                break

    items = []
    total_num = 0
    for it in plan_items:
        plan_num = _i(it.get("planNum"))
        total_num += plan_num or 0
        items.append({
            "sku": _s(it.get("sku")),
            "commodityId": _s(it.get("commodityId")),
            "num": _num_str(plan_num),
            "perPurchase": _num_str(_f(it.get("purchaseCost"))),
            # 关联用明细级采购计划号 planNo(PP…)，不是组号 planGroupNo(PPG…)，
            # 否则赛狐无法标记具体哪条计划被采购，purchaseStatus 不更新
            "purchasePlanNo": _s(it.get("planNo")) or plan_group_no,
            "expectArrivalTime": _arrival_date(it.get("expectArrivalTime")),
        })

    body = {
        "warehouseId": warehouse_id,
        "action": action,
        "includeTax": "0",
        "supplierId": supplier_id,
        "items": items,
    }
    return {
        "body": body,
        "supplier_name": supplier_name,
        "warehouse_name": warehouse_name,
        "brand_name": brand_name,
        "item_count": len(items),
        "total_num": total_num,
    }


_PO_NO_KEYS = ("purchaseOrderNo", "purchaseNo", "purchaseNoStr", "orderNo")


def _extract_purchase_no(data):
    """从赛狐 create.json 返回里抽采购单号。

    实测返回 data 是 list[{purchaseOrderNo, id, ...}]（单号字段=purchaseOrderNo，
    不是 purchaseNo），也兼容 dict 单对象 / {list:[...]} / 纯字符串列表。
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        nos = []
        for x in data:
            if isinstance(x, dict):
                no = next((str(x[k]) for k in _PO_NO_KEYS if x.get(k)), "")
                if no:
                    nos.append(no)
            elif x:
                nos.append(str(x))
        return ",".join(nos)
    if isinstance(data, dict):
        # 可能套一层 list/rows/records
        for lk in ("list", "rows", "records", "data", "purchaseList"):
            if isinstance(data.get(lk), list):
                got = _extract_purchase_no(data[lk])
                if got:
                    return got
        for k in (*_PO_NO_KEYS, "purchaseNos", "purchaseNoList"):
            v = data.get(k)
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v if x)
            if v:
                return str(v)
    return ""


def _plan_key(no):
    """采购计划号的数字部分（PPG2606100003 / PP2606100003 → 2606100003），
    用于跨组号/明细号匹配。"""
    m = re.search(r"\d+", str(no or ""))
    return m.group() if m else ""


def _existing_po_for_plan(plan_group_no, days=45):
    """赛狐近期采购单里关联此采购计划、且未取消的 PO 号（防重用）。

    page.json 不支持按采购计划号过滤，只能拉近 days 天采购单列表逐个比对
    purchasePlanNoList。采购单里存的是明细号(PP)，入参可能是组号(PPG)，
    故按数字部分匹配。best-effort：异常/查不到都返回 []（不阻断生成）。
    """
    target = _plan_key(plan_group_no)
    end = datetime.now()
    start = end - timedelta(days=days)
    found = []
    try:
        for page in (1, 2, 3):
            resp = sf.call(PAGE_PO_PATH, {
                "pageNo": str(page), "pageSize": "100",
                "createTimeStart": start.strftime("%Y-%m-%d 00:00:00"),
                "createTimeEnd": end.strftime("%Y-%m-%d 23:59:59"),
            })
            rows = (resp or {}).get("rows") or []
            for r in rows:
                pol = r.get("purchasePlanNoList") or []
                if target and any(_plan_key(x) == target for x in pol) and _i(r.get("status")) != 3:
                    no = r.get("purchaseNo")
                    if no and no not in found:
                        found.append(no)
            if len(rows) < 100:
                break
    except Exception:
        pass
    return found


def create_purchase_order(db, plan_group_no, action="0", dry_run=True, force=False,
                          auto_arrival=False):
    """生成采购单。

    dry_run=True  → 仅返回 build_po_body 结果（不调赛狐），供预览。
    dry_run=False → 先防重（除非 force）：本地已记采购单号 / 赛狐已有关联此计划的未取消单
                    → RuntimeError。否则调赛狐 create.json，成功后更新 PurchasePlanConfirm。
    auto_arrival=True 且 action=2 → 建单下单后再按采购量全部良品自动到货，一步到「已完成」。
    找不到采购计划返回 None。
    """
    assembled = build_po_body(db, plan_group_no, action=action)
    if assembled is None:
        return None
    body = assembled["body"]

    if dry_run:
        return assembled

    if not force:
        rec0 = (db.query(PurchasePlanConfirm)
                .filter(PurchasePlanConfirm.plan_group_no == plan_group_no).first())
        if rec0 is not None and rec0.purchase_no:
            raise RuntimeError(
                f"该采购计划本地已记录采购单 {rec0.purchase_no}，"
                f"如需重新生成请先作废，或勾选「强制生成」。")
        dup = _existing_po_for_plan(plan_group_no)
        if dup:
            raise RuntimeError(
                f"赛狐已有关联此采购计划的采购单 {','.join(dup)}（未取消），"
                f"重复生成会建出重复单。请先作废这些单，或勾选「强制生成」。")

    resp = sf.call(CREATE_PO_PATH, body)
    purchase_no = _extract_purchase_no(resp)

    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no)
           .first())
    if rec is None:
        rec = PurchasePlanConfirm(plan_group_no=plan_group_no,
                                  brand_name=assembled["brand_name"])
        db.add(rec)
    # action=2「提交并下单」直接进待到货；0草稿/1提交进待下单
    rec.status = ST_SHIPPING if str(action) == "2" else ST_ORDERED
    rec.supplier_id = _s(body.get("supplierId"))
    rec.warehouse_id = _s(body.get("warehouseId"))
    rec.purchase_no = purchase_no
    rec.po_action = action
    rec.po_created_at = datetime.now()
    db.commit()

    result = {"purchase_no": purchase_no, "sellfox_resp": resp, "body": body, "status": rec.status}
    # 一步到底：action=2 已下单(待到货)，再按采购量全部良品自动到货 → 已完成
    if auto_arrival and str(action) == "2" and purchase_no:
        arrival_items = [{"sku": _s(i.get("sku")), "goods": i.get("num"), "defective": 0}
                         for i in body.get("items", []) if _s(i.get("sku"))]
        try:
            result["arrival"] = confirm_arrival(db, plan_group_no, arrival_items, arrival_type=0)
            result["status"] = ST_DONE
        except RuntimeError as e:
            result["arrival_error"] = str(e)   # 到货失败则停在待到货，前端提示
    return result


def _require_confirm(db, plan_group_no):
    """取确认记录，无 / 无采购单号则 RuntimeError。"""
    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no)
           .first())
    if rec is None:
        raise RuntimeError(f"采购计划 {plan_group_no} 还没有确认记录")
    if not rec.purchase_no:
        raise RuntimeError(f"采购计划 {plan_group_no} 还没有生成采购单")
    return rec


def submit_order(db, plan_group_no):
    """下单：待下单 → 待到货。调赛狐 /api/purchase/order.json {purchaseNos:[采购单号]}。

    采购单号可能逗号分隔多个，全部一起下单。成功后 status=待到货。
    """
    rec = _require_confirm(db, plan_group_no)
    nos = [n.strip() for n in rec.purchase_no.split(",") if n.strip()]
    resp = sf.call(ORDER_PO_PATH, {"purchaseNos": nos})
    rec.status = ST_SHIPPING
    db.commit()
    return {"purchase_no": rec.purchase_no, "status": rec.status, "sellfox_resp": resp}


def confirm_arrival(db, plan_group_no, items, arrival_type=0):
    """到货：待到货 → 已完成。调赛狐 /api/purchase/arrival.json。

    items = [{sku, goods(良品数), defective(次品数)}]；goods/defective 转字符串。
    采购单号若多个，取第一个（到货接口单号单值）。成功后 status=已完成。
    """
    rec = _require_confirm(db, plan_group_no)
    no = rec.purchase_no.split(",")[0].strip()
    norm = []
    for it in (items or []):
        sku = _s(it.get("sku"))
        if not sku:
            continue
        line = {
            "sku": sku,
            "goods": _num_str(it.get("goods", 0)),
            "defective": _num_str(it.get("defective", 0)),
        }
        if _s(it.get("fnSku")):
            line["fnSku"] = _s(it.get("fnSku"))
        if _s(it.get("shopName")):
            line["shopName"] = _s(it.get("shopName"))
        if _s(it.get("arrivalRemark")):
            line["arrivalRemark"] = _s(it.get("arrivalRemark"))
        norm.append(line)
    if not norm:
        raise RuntimeError("到货明细为空")
    body = {"purchaseNo": no, "items": norm, "arrivalType": int(arrival_type or 0)}
    resp = sf.call(ARRIVAL_PO_PATH, body)
    rec.status = ST_DONE
    db.commit()
    return {"purchase_no": rec.purchase_no, "status": rec.status, "sellfox_resp": resp, "body": body}


def cancel_order(db, plan_group_no):
    """作废采购单：逐个调赛狐 cancel.json，全部成功则本地回退到「待采购」。

    采购单号取本地记录；本地没有则从赛狐近期单兜底找（防本地记录被删）。
    已完成(status=2)的单赛狐通常拒绝取消，会进 failed，本地保留不回退。
    """
    rec = (db.query(PurchasePlanConfirm)
           .filter(PurchasePlanConfirm.plan_group_no == plan_group_no).first())
    nos = []
    if rec is not None and rec.purchase_no:
        nos = [n.strip() for n in rec.purchase_no.split(",") if n.strip()]
    if not nos:
        nos = _existing_po_for_plan(plan_group_no)
    if not nos:
        raise RuntimeError(f"采购计划 {plan_group_no} 没有可作废的采购单")

    ok, failed = [], []
    for no in nos:
        try:
            sf.call(CANCEL_PO_PATH, {"purchaseNo": no})
            ok.append(no)
        except RuntimeError as e:
            failed.append({"purchase_no": no, "error": str(e)})

    if rec is not None:
        if failed:
            # 部分失败：仅移除已作废的，状态不回退
            rec.purchase_no = ",".join(n for n in nos if n not in ok)
        else:
            rec.purchase_no = ""
            rec.po_action = ""
            rec.po_created_at = None
            rec.status = ST_CONFIRMED      # 回到待采购，可重新生成
        db.commit()
    return {"cancelled": ok, "failed": failed,
            "status": rec.status if rec is not None else "", "plan_group_no": plan_group_no}
