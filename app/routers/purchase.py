"""采购单路由：工厂确认 → 生成赛狐采购单。

路径不带 /api 前缀，main.py 统一加（app.include_router(..., prefix="/api")）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import purchase_order_service as pos

router = APIRouter()


@router.post("/purchase/confirm")
def confirm(data: dict, db: Session = Depends(get_db)):
    """人工工厂确认 → upsert 确认记录（status=已确认）。

    body: {plan_group_no, items, confirmed_by?, remark?}
    """
    data = data or {}
    pgn = data.get("plan_group_no", "")
    if not pgn:
        raise HTTPException(400, "缺少 plan_group_no")
    items = data.get("items") or []
    try:
        return pos.confirm_plan(
            db, pgn, items,
            confirmed_by=data.get("confirmed_by", "") or "",
            remark=data.get("remark", "") or "",
        )
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.get("/purchase/confirm/{plan_group_no}")
def get_confirm(plan_group_no: str, db: Session = Depends(get_db)):
    """查询确认 + 采购单状态。无记录 → {plan_group_no, status:'待审核'}。"""
    return pos.get_confirm(db, plan_group_no)


@router.delete("/purchase/confirm/{plan_group_no}")
def delete_confirm(plan_group_no: str, db: Session = Depends(get_db)):
    """删除本地采购确认记录（不影响赛狐采购单 PO）。"""
    try:
        return pos.delete_confirm(db, plan_group_no)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/purchase/create-order")
def create_order(data: dict, db: Session = Depends(get_db)):
    """生成采购单。

    body: {plan_group_no, action?(默认"0"), dry_run?(默认true)}
    dry_run=true 仅组装预览（不调赛狐）；false 真实创建。
    """
    data = data or {}
    pgn = data.get("plan_group_no", "")
    if not pgn:
        raise HTTPException(400, "缺少 plan_group_no")
    action = str(data.get("action", "0") or "0")
    dry_run = data.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "")
    force = bool(data.get("force", False))
    auto_arrival = bool(data.get("auto_arrival", False))
    try:
        res = pos.create_purchase_order(db, pgn, action=action, dry_run=bool(dry_run),
                                        force=force, auto_arrival=auto_arrival)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))
    if res is None:
        raise HTTPException(404, f"采购计划 {pgn} 未找到（近 60 天）")
    return res


@router.post("/purchase/cancel-order")
def cancel_order(data: dict, db: Session = Depends(get_db)):
    """作废采购单（赛狐写）：成功后本地回退到「待采购」。body: {plan_group_no}。"""
    pgn = (data or {}).get("plan_group_no", "")
    if not pgn:
        raise HTTPException(400, "缺少 plan_group_no")
    try:
        return pos.cancel_order(db, pgn)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/purchase/submit-order")
def submit_order(data: dict, db: Session = Depends(get_db)):
    """下单（赛狐写）：待下单 → 待到货。body: {plan_group_no}。"""
    pgn = (data or {}).get("plan_group_no", "")
    if not pgn:
        raise HTTPException(400, "缺少 plan_group_no")
    try:
        return pos.submit_order(db, pgn)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/purchase/arrival")
def arrival(data: dict, db: Session = Depends(get_db)):
    """到货（赛狐写）：待到货 → 已完成。

    body: {plan_group_no, items:[{sku, goods, defective}], arrival_type?(默认0)}
    """
    data = data or {}
    pgn = data.get("plan_group_no", "")
    if not pgn:
        raise HTTPException(400, "缺少 plan_group_no")
    try:
        return pos.confirm_arrival(
            db, pgn, data.get("items") or [],
            arrival_type=data.get("arrival_type", 0) or 0)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))
