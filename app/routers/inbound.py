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
            "placement_option_id": r.placement_option_id,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}


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
           status?, error?, items?, shipments?, placement_option_id?}
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
    db.commit()
    db.refresh(r)
    return _rec_dict(r)
