"""建仓辅助路由（2026-07-29 起建仓执行改走 mcapi 的赛狐建仓 /api/v1/sellfox/inbound/*）。

本路由只保留三类能力：
1. 输入准备：补仓 Excel 解析 / 从赛狐采购计划取明细（供 Codex 组装 mcapi 建仓入参）
2. 建仓过程记录（断点续跑）：InboundPlan 表复用为轻量记录——Codex 每推进一步 upsert，
   中断后凭 sellfox_plan_id/shop_id 到 mcapi 查状态接着建
3. 记录查询：列表/详情（前端建仓记录列表沿用 GET /inbound/plans）

红线不变：无任何取消/删除接口；建仓完成后货件信息经 POST /api/sync/import 从赛狐拉取。
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import mcapi_inbound_client as mcapi
from ..database import get_db
from ..models import InboundPlan
from ..services import inbound_service as ibs

router = APIRouter()


@router.post("/inbound/parse-excel")
def parse_excel(file: UploadFile = File(...)):
    """上传补仓计划 Excel → 明细行（长宽高 in / 重 lb 直读；喂赛狐建仓前需换算 cm/kg）。"""
    data = file.file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        items = ibs.parse_replenishment_excel(data)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"items": items}


@router.get("/inbound/from-purchase-plan/{plan_group_no}")
def from_purchase_plan(plan_group_no: str, db: Session = Depends(get_db)):
    """从赛狐采购计划取建仓明细（箱规留空由产品库/赛狐补）。"""
    try:
        res = ibs.items_from_purchase_plan(db, plan_group_no)
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return res


def _rec_dict(r: InboundPlan):
    def _j(v):
        try:
            return json.loads(v) if v else None
        except (ValueError, TypeError):
            return None
    return {"id": r.id, "name": r.name, "source_type": r.source_type,
            "source_ref": r.source_ref, "brand_id": r.brand_id,
            "shop_id": r.store,                       # 复用 store 列存赛狐店铺 ID
            "sellfox_plan_id": r.amazon_inbound_plan_id,  # 复用列存赛狐建仓计划 ID
            "status": r.status, "error": r.error,
            "items": _j(r.items_snapshot), "shipments": _j(r.shipments_snapshot),
            "source_address": _j(r.source_address),
            "packing_option_id": r.packing_option_id,
            "placement_option_id": r.placement_option_id,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}


def _workflow_state(r: InboundPlan):
    try:
        state = json.loads(r.shipments_snapshot) if r.shipments_snapshot else {}
    except (ValueError, TypeError):
        state = {}
    return state if isinstance(state, dict) else {"shipments": state}


def _save_state(db, r, state, status=None, error=None):
    r.shipments_snapshot = json.dumps(state, ensure_ascii=False)
    if status is not None:
        r.status = status
    if error is not None:
        r.error = error
    db.commit()
    db.refresh(r)


def _mcapi_http_error(exc):
    return HTTPException(exc.status_code, str(exc))


@router.get("/inbound/shops")
def list_sellfox_shops():
    """从 MCAPI 获取赛狐店铺 ID；建仓不得把本地 amazon_store key 当 shop_id。"""
    try:
        return {"shops": mcapi.list_shops() or []}
    except mcapi.McapiInboundError as exc:
        raise _mcapi_http_error(exc)


@router.post("/inbound/build/start")
def start_sellfox_build(data: dict, db: Session = Depends(get_db)):
    """建仓第一段：占坑→建计划→装箱→生成分仓方案，然后强制停下等待人工选择。

    body: {plan_group_no, brand_id, shop_id, items?, name?, source_type?}
    ``items`` 省略时从采购计划读取。该接口绝不确认分仓或运输。
    """
    data = data or {}
    ppg = str(data.get("plan_group_no") or "").strip()
    brand_id = data.get("brand_id")
    shop_id = data.get("shop_id")
    if not ppg or not brand_id or shop_id in (None, ""):
        raise HTTPException(400, "缺少 plan_group_no、brand_id 或 shop_id")
    existing = (db.query(InboundPlan)
                .filter(InboundPlan.source_ref == ppg,
                        InboundPlan.amazon_inbound_plan_id != "")
                .first())
    if existing:
        raise HTTPException(
            409,
            f"{ppg} 已有建仓记录 {existing.id} / {existing.amazon_inbound_plan_id}，禁止重复创建",
        )
    try:
        source_address = ibs._source_address(db, int(brand_id))
        raw_items = data.get("items")
        source_type = str(data.get("source_type") or "purchase_plan")
        default_name = ppg
        if not raw_items:
            prepared = ibs.items_from_purchase_plan(db, ppg)
            raw_items = prepared.get("items") or []
            default_name = prepared.get("name") or ppg
        items = ibs._resolve_items(db, raw_items)
        box_specs = ibs.box_specs_from_items(items)
        name = str(data.get("name") or default_name or ppg).strip()
        if len(name) > 40:
            raise RuntimeError("赛狐建仓计划名称不能超过 40 个字符")
    except (RuntimeError, TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc))

    try:
        mcapi.claim_build(
            f"build:{ppg}", refs={"plan_group_no": ppg, "shop_id": str(shop_id)}
        )
        created = mcapi.create_plan(
            shop_id,
            name,
            source_address,
            [{"msku": i["msku"], "quantity": i["quantity"]} for i in items],
        ) or {}
    except mcapi.McapiInboundError as exc:
        raise _mcapi_http_error(exc)
    plan_id = str(created.get("inbound_plan_id") or created.get("plan_id") or "").strip()
    owners = created.get("owners") or {}
    if not plan_id or not owners:
        raise HTTPException(502, f"MCAPI 创建计划返回缺少 inbound_plan_id/owners：{created}")

    r = InboundPlan(
        source_type=source_type,
        source_ref=ppg,
        brand_id=int(brand_id),
        store=str(shop_id),
        name=name,
        status="计划已创建",
        amazon_inbound_plan_id=plan_id,
        items_snapshot=json.dumps(items, ensure_ascii=False),
        source_address=json.dumps(source_address, ensure_ascii=False),
        created_at=datetime.now(),
    )
    state = {
        "owners": owners,
        "box_specs": box_specs,
        "packing": None,
        "placement_options": [],
        "shipments": [],
    }
    db.add(r)
    _save_state(db, r, state, status="计划已创建", error="")

    try:
        packing = mcapi.submit_packing(plan_id, box_specs, owners) or {}
        state["packing"] = packing
        r.packing_option_id = str(packing.get("packing_option_id") or "")
        _save_state(db, r, state, status="装箱已提交", error="")
        options = mcapi.list_placements(plan_id, shop_id, regenerate=False) or []
        state["placement_options"] = options
        _save_state(db, r, state, status="分仓方案已生成", error="")
    except mcapi.McapiInboundError as exc:
        _save_state(db, r, state, error=str(exc))
        raise _mcapi_http_error(exc)
    return {
        "record": _rec_dict(r),
        "placement_options": state["placement_options"],
        "requires_selection": True,
        "message": "已生成分仓方案，流程已暂停；请选择方案后再调用 finalize",
    }


@router.post("/inbound/build/{rec_id}/resume-to-placement")
def resume_to_placement(rec_id: int, data: dict | None = None,
                        db: Session = Depends(get_db)):
    """断点续跑到分仓方案停点；不会确认任何方案。"""
    r = db.get(InboundPlan, rec_id)
    if r is None or not r.amazon_inbound_plan_id:
        raise HTTPException(404, f"建仓记录 {rec_id} 不存在或没有远端计划号")
    state = _workflow_state(r)
    owners = state.get("owners") or {}
    box_specs = state.get("box_specs") or []
    try:
        if not state.get("packing"):
            packing = mcapi.submit_packing(r.amazon_inbound_plan_id, box_specs, owners) or {}
            state["packing"] = packing
            r.packing_option_id = str(packing.get("packing_option_id") or "")
            _save_state(db, r, state, status="装箱已提交", error="")
        regenerate = bool((data or {}).get("regenerate"))
        options = mcapi.list_placements(
            r.amazon_inbound_plan_id, r.store, regenerate=regenerate
        ) or []
        state["placement_options"] = options
        _save_state(db, r, state, status="分仓方案已生成", error="")
    except mcapi.McapiInboundError as exc:
        _save_state(db, r, state, error=str(exc))
        raise _mcapi_http_error(exc)
    return {
        "record": _rec_dict(r),
        "placement_options": state["placement_options"],
        "requires_selection": True,
    }


@router.post("/inbound/build/{rec_id}/finalize")
def finalize_sellfox_build(rec_id: int, data: dict, db: Session = Depends(get_db)):
    """建仓第二段：仅在用户明确选择方案后确认分仓并锁定运输。"""
    r = db.get(InboundPlan, rec_id)
    if r is None or not r.amazon_inbound_plan_id:
        raise HTTPException(404, f"建仓记录 {rec_id} 不存在或没有远端计划号")
    data = data or {}
    option_id = str(data.get("placement_option_id") or "").strip()
    ready = str(data.get("ready_to_ship_start") or "").strip()
    if not option_id or not ready:
        raise HTTPException(400, "缺少 placement_option_id 或 ready_to_ship_start")
    state = _workflow_state(r)
    offered = {
        str(o.get("placement_option_id"))
        for o in (state.get("placement_options") or [])
        if o.get("placement_option_id")
    }
    if option_id not in offered:
        raise HTTPException(409, "所选分仓方案不在当前方案快照中；请先刷新方案，禁止猜测 ID")
    payload = {
        "placement_option_id": option_id,
        "shop_id": int(r.store),
        "ready_to_ship_start": ready,
        "shipping_mode": str(data.get("shipping_mode") or "FREIGHT_LTL"),
        "carrier_name": str(data.get("carrier_name") or "Other"),
    }
    if data.get("delivery_window_start"):
        payload["delivery_window_start"] = str(data["delivery_window_start"])
    try:
        result = mcapi.finalize(r.amazon_inbound_plan_id, payload) or {}
    except mcapi.McapiInboundError as exc:
        _save_state(db, r, state, error=str(exc))
        raise _mcapi_http_error(exc)
    state["shipments"] = result.get("shipments") or []
    state["finalize"] = result
    r.placement_option_id = option_id
    _save_state(db, r, state, status="运输已锁定", error="")
    return {
        "record": _rec_dict(r),
        "shipments": state["shipments"],
        "next_action": "调用 /api/sync/plans 找到该 STA 后，再 POST /api/sync/import 导入批次",
    }


@router.get("/inbound/build/{rec_id}/remote")
def remote_build_state(rec_id: int, db: Session = Depends(get_db)):
    """只读查询 MCAPI/赛狐实际进度，供断点续跑判断。"""
    r = db.get(InboundPlan, rec_id)
    if r is None or not r.amazon_inbound_plan_id:
        raise HTTPException(404, f"建仓记录 {rec_id} 不存在或没有远端计划号")
    try:
        remote = mcapi.get_plan(r.amazon_inbound_plan_id, r.store)
    except mcapi.McapiInboundError as exc:
        raise _mcapi_http_error(exc)
    return {"record": _rec_dict(r), "remote": remote}


@router.get("/inbound/plans")
def list_records(db: Session = Depends(get_db)):
    """建仓过程记录列表（断点续跑用；旧路径沿用，前端记录列表不改）。"""
    rows = db.query(InboundPlan).order_by(InboundPlan.id.desc()).limit(100).all()
    return {"plans": [_rec_dict(r) for r in rows]}


@router.get("/inbound/plans/{rec_id}")
def get_record(rec_id: int, db: Session = Depends(get_db)):
    r = db.get(InboundPlan, rec_id)
    if r is None:
        raise HTTPException(404, f"记录 {rec_id} 不存在")
    return _rec_dict(r)


@router.post("/inbound/records")
def upsert_record(data: dict, db: Session = Depends(get_db)):
    """建仓过程记录 upsert（Codex 每推进一步调一次，断点续跑的本地事实源）。

    body: {sellfox_plan_id?, name?, shop_id?, source_type?, source_ref?, brand_id?,
           status?, error?, items?, shipments?, source_address?, packing_option_id?,
           placement_option_id?}
    定位优先级：sellfox_plan_id（有值则按它 upsert）> id。状态自由文本，建议：
    计划已创建/装箱已提交/分仓方案已生成/已选方案/运输已锁定/已导入批次/失败。
    """
    data = data or {}
    r = None
    spid = (data.get("sellfox_plan_id") or "").strip()
    if spid:
        r = (db.query(InboundPlan)
             .filter(InboundPlan.amazon_inbound_plan_id == spid).first())
    if r is None and data.get("id"):
        r = db.get(InboundPlan, data["id"])
    if r is None:
        r = InboundPlan(created_at=datetime.now())
        db.add(r)
    if spid:
        r.amazon_inbound_plan_id = spid
    for k in ("name", "source_type", "source_ref", "brand_id",
              "status", "error", "placement_option_id"):
        if data.get(k) is not None:
            setattr(r, k, data[k])
    if data.get("shop_id") is not None:
        r.store = str(data["shop_id"])
    if data.get("items") is not None:
        r.items_snapshot = json.dumps(data["items"], ensure_ascii=False)
    if data.get("shipments") is not None:
        r.shipments_snapshot = json.dumps(data["shipments"], ensure_ascii=False)
    if data.get("source_address") is not None:
        r.source_address = json.dumps(data["source_address"], ensure_ascii=False)
    if data.get("packing_option_id") is not None:
        r.packing_option_id = str(data["packing_option_id"])
    db.commit()
    db.refresh(r)
    return _rec_dict(r)
