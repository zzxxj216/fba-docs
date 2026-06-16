"""建仓（亚马逊 FBA STA 入库计划）路由。

各 generate/confirm 步骤在 service 层内部已轮询 operationId 到 SUCCESS 再返回，
前端按向导逐步调用即可。路径不带 /api 前缀，main.py 统一加。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import inbound_service as ibs

router = APIRouter()


@router.get("/inbound/plans")
def list_plans(db: Session = Depends(get_db)):
    """本地建仓记录列表。"""
    return ibs.list_plans(db)


@router.get("/inbound/plans/{rec_id}")
def get_plan(rec_id: int, db: Session = Depends(get_db)):
    try:
        return ibs.get_plan(db, rec_id)
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@router.post("/inbound/create")
def create(data: dict, db: Session = Depends(get_db)):
    """① 创建入库计划。body: {brand_id, items:[{msku,quantity,units_per_box?,l_in?,w_in?,h_in?,weight_lb?,expiration?}], source_type?, source_ref?, name?}。"""
    data = data or {}
    items = data.get("items") or []
    if not items:
        raise HTTPException(400, "缺 items（建仓明细）")
    try:
        return ibs.create_plan(
            db, data.get("brand_id"), items,
            source_type=data.get("source_type", "manual") or "manual",
            source_ref=data.get("source_ref", "") or "",
            name=data.get("name", "") or "")
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


def _step(fn, rec_id, db):
    try:
        return fn(db, rec_id)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/inbound/plans/{rec_id}/packing")
def packing(rec_id: int, db: Session = Depends(get_db)):
    """② 生成并确认装箱方案。"""
    return _step(ibs.gen_confirm_packing, rec_id, db)


@router.post("/inbound/plans/{rec_id}/boxes")
def boxes(rec_id: int, db: Session = Depends(get_db)):
    """③ 提交箱规（按每箱数分箱）。"""
    return _step(ibs.submit_boxes, rec_id, db)


@router.post("/inbound/plans/{rec_id}/placement")
def placement(rec_id: int, db: Session = Depends(get_db)):
    """④ 生成分仓方案（耗时较长）。"""
    return _step(ibs.gen_placement, rec_id, db)


@router.get("/inbound/plans/{rec_id}/placements")
def placements(rec_id: int, db: Session = Depends(get_db)):
    """列分仓方案（目的仓 FC + 费用）供选。"""
    try:
        return ibs.list_placements(db, rec_id)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/inbound/plans/{rec_id}/choose")
def choose(rec_id: int, data: dict, db: Session = Depends(get_db)):
    """⑤ 确认分仓方案（生成货件）。body: {placement_option_id}。"""
    poid = (data or {}).get("placement_option_id", "")
    if not poid:
        raise HTTPException(400, "缺 placement_option_id")
    try:
        return ibs.choose_placement(db, rec_id, poid)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/inbound/plans/{rec_id}/cancel")
def cancel(rec_id: int, db: Session = Depends(get_db)):
    """取消建仓（取消亚马逊入库计划 + 本地标记）。"""
    return _step(ibs.cancel_plan, rec_id, db)


@router.get("/inbound/operation/{operation_id}")
def operation(operation_id: str, store: str = "", db: Session = Depends(get_db)):
    """前端轮询长操作状态（转发 mcapi）。"""
    try:
        return ibs.operation_status(None, operation_id, store=store or None)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
