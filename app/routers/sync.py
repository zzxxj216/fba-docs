"""赛狐同步路由（含历史批次 Excel 离线导入）。"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import import_service, purchase_plan_service, sync_service

router = APIRouter()


@router.get("/sync/plans")
def list_plans(page: int = 1, db: Session = Depends(get_db)):
    """赛狐 STA 入库计划列表（分页，每行附 shop_name/marketplace/imported/batch_id）。"""
    try:
        return sync_service.list_plans(db, page)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/sync/import")
def import_plan(data: dict, db: Session = Depends(get_db)):
    """导入/增量更新一个批次 → {"batch_id", "report"}。"""
    inbound_plan_id = (data or {}).get("inbound_plan_id", "")
    if not inbound_plan_id:
        raise HTTPException(400, "缺少 inbound_plan_id")
    try:
        return sync_service.import_batch(db, inbound_plan_id)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


@router.post("/sync/import-excel")
async def import_excel(file: UploadFile = File(...),
                       batch_name: str = Form(""),
                       db: Session = Depends(get_db)):
    """历史批次离线导入：赛狐「发货单导出」xlsx（API 拉不到的老批次）。

    multipart: file=发货单 xlsx；可选 batch_name 覆盖自动命名。
    返回 {"batch_id", "created", "report"}（同 /sync/import）。
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "上传文件为空")
    try:
        return import_service.import_shipping_order(
            db, content, filename=file.filename or "", batch_name=batch_name or "")
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------- 采购计划

@router.get("/sync/purchase-plans")
def list_purchase_plans(page: int = 1, status: str = "", db: Session = Depends(get_db)):
    """列采购计划（近 60 天，全部状态）。status=待审核/待采购/已采购 时按状态过滤
    → {plans, page, total_size, status_counts}。"""
    try:
        return purchase_plan_service.list_plans(db, page, status=status or None)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/sync/purchase-plans/{plan_group_no}/candidates")
def purchase_plan_candidates(plan_group_no: str, db: Session = Depends(get_db)):
    """为采购计划找配套 STA 入库计划 → {auto_inbound_plan_id, candidates}。"""
    try:
        res = purchase_plan_service.candidates(db, plan_group_no)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if res is None:
        raise HTTPException(404, f"采购计划 {plan_group_no} 未找到（近 60 天已审批）")
    return res


@router.post("/sync/purchase-plans/preview")
def purchase_plan_preview(data: dict, db: Session = Depends(get_db)):
    """组装采购计划+STA 完整预览（不落库）。"""
    pgn = (data or {}).get("plan_group_no", "")
    ipid = (data or {}).get("inbound_plan_id", "")
    if not pgn or not ipid:
        raise HTTPException(400, "缺少 plan_group_no 或 inbound_plan_id")
    try:
        res = purchase_plan_service.preview(db, pgn, ipid)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if res is None:
        raise HTTPException(404, f"采购计划 {pgn} 未找到（近 60 天已审批）")
    return res


@router.post("/sync/purchase-plans/import")
def purchase_plan_import(data: dict, db: Session = Depends(get_db)):
    """导入采购计划配套 STA 成批次 → {batch_id, report, purchase_plan_no, ...}。"""
    pgn = (data or {}).get("plan_group_no", "")
    ipid = (data or {}).get("inbound_plan_id", "")
    if not pgn or not ipid:
        raise HTTPException(400, "缺少 plan_group_no 或 inbound_plan_id")
    try:
        res = purchase_plan_service.import_plan(db, pgn, ipid)
    except RuntimeError as e:
        db.rollback()
        raise HTTPException(502, str(e))
    if res is None:
        raise HTTPException(404, f"采购计划 {pgn} 未找到（近 60 天已审批）")
    return res
