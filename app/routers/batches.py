"""批次/货件/明细路由：列表、全量详情、校验、编辑（edited_fields 登记）、打包下载。"""

import io
import json
import os
import shutil
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import OUTPUT_DIR, get_db
from ..models import Batch, Forwarder, GeneratedDoc, Shipment, ShipmentItem, Template
from ..services import validate_service, batch_prep_service
from .. import sop_flow

router = APIRouter()


def to_dict(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ---------------------------------------------------------------- 批次

@router.get("/batches")
def list_batches(db: Session = Depends(get_db)):
    """批次列表（带 brand_name / shipment_count / 状态）。"""
    out = []
    for b in db.query(Batch).order_by(Batch.id.desc()).all():
        d = to_dict(b)
        d["brand_name"] = b.brand.name if b.brand else ""
        d["company_name"] = b.company.name_cn if b.company else ""
        d["factory_name"] = b.factory.name if b.factory else ""
        d["shipment_count"] = len(b.shipments)
        out.append(d)
    return out


@router.get("/batches/{batch_id}/full")
def batch_full(batch_id: int, db: Session = Depends(get_db)):
    """批次 + 货件(含明细/逐箱/货代名) + 生成记录 + 汇总。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")

    forwarders = {f.id: f.name for f in db.query(Forwarder).all()}
    result = to_dict(b)
    result["brand_name"] = b.brand.name if b.brand else ""
    result["brand_abbr2"] = b.brand.abbr2 if b.brand else ""
    result["company_name"] = b.company.name_cn if b.company else ""
    result["factory_name"] = b.factory.name if b.factory else ""

    total_box = total_qty = 0
    total_gross = total_value = 0.0
    shipments = []
    for sp in b.shipments:
        sd = to_dict(sp)
        sd["edited_fields"] = json.loads(sp.edited_fields or "[]")
        sd["forwarder_name"] = forwarders.get(sp.forwarder_id, "")
        items = []
        s_box = s_qty = 0
        s_gross = s_value = 0.0
        for it in sp.items:
            idd = to_dict(it)
            idd["edited_fields"] = json.loads(it.edited_fields or "[]")
            p = it.product
            idd["product_name"] = p.name_customs_cn if p else ""
            idd["product_name_en"] = p.name_customs_en if p else ""
            idd["box_weight_kg"] = p.box_weight_kg if p else None
            items.append(idd)
            s_box += it.box_count or 0
            s_qty += it.qty or 0
            if p and p.box_weight_kg and it.box_count:
                s_gross += it.box_count * p.box_weight_kg
            if it.qty and it.customs_unit_price:
                s_value += it.qty * it.customs_unit_price
        sd["items"] = items
        sd["boxes"] = [to_dict(box) for box in sp.boxes]
        sd["totals"] = {"box_count": s_box, "qty": s_qty,
                        "gross_weight": round(s_gross, 2),
                        "value": round(s_value, 2)}
        shipments.append(sd)
        total_box += s_box
        total_qty += s_qty
        total_gross += s_gross
        total_value += s_value

    result["shipments"] = shipments
    result["docs"] = [to_dict(d) for d in db.query(GeneratedDoc)
                      .filter(GeneratedDoc.batch_id == batch_id)
                      .order_by(GeneratedDoc.id.desc()).all()]
    result["totals"] = {"box_count": total_box, "qty": total_qty,
                        "gross_weight": round(total_gross, 2),
                        "value": round(total_value, 2)}

    # 生成区默认勾选：本批货代+内部的启用模板；店铺配了 doc_types 清单则再按文件类型过滤。
    forwarder_ids = {sp.forwarder_id for sp in b.shipments if sp.forwarder_id}
    shop_types = None
    if b.company and b.company.doc_types and b.company.doc_types.strip():
        try:
            parsed = json.loads(b.company.doc_types)
            if isinstance(parsed, list) and parsed:
                shop_types = set(parsed)
        except (ValueError, TypeError):
            shop_types = None
    suggested = []
    for t in db.query(Template).filter(Template.active.is_(True)).order_by(Template.id).all():
        if not (t.owner_type == "internal" or t.forwarder_id in forwarder_ids):
            continue
        if shop_types is not None and t.doc_type not in shop_types:
            continue
        suggested.append(t.id)
    result["suggested_template_ids"] = suggested
    result["sop"] = sop_flow.compute(db, b)
    return result


@router.get("/batches/{batch_id}/prep")
def batch_prep(batch_id: int, db: Session = Depends(get_db)):
    """SOP 第一步「数据准备」：补仓清单 + 建仓清单 + 校验。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    return batch_prep_service.aggregate(db, b)


@router.post("/batches/{batch_id}/prep/fill-products")
def batch_fill_products(batch_id: int, db: Session = Depends(get_db)):
    """一键从赛狐商品接口给未匹配 SKU 自动建档，再返回最新聚合。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    res = batch_prep_service.fill_missing_products(db, b)
    res["prep"] = batch_prep_service.aggregate(db, b)
    return res


@router.post("/batches/{batch_id}/sop")
def toggle_sop(batch_id: int, data: dict, db: Session = Depends(get_db)):
    """勾选/取消一个 SOP 手动步骤。body: {step_key, done}。返回最新 SOP 进度。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    data = data or {}
    try:
        return sop_flow.toggle(db, b, data.get("step_key", ""), bool(data.get("done", True)))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/batches/{batch_id}/validate")
def batch_validate(batch_id: int, db: Session = Depends(get_db)):
    return validate_service.validate_batch(db, batch_id)


@router.put("/batches/{batch_id}")
def update_batch(batch_id: int, data: dict, db: Session = Depends(get_db)):
    """编辑批次（contract_date、factory_id、name、status 等）。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    allowed = {"name", "brand_id", "company_id", "factory_id", "shop_name",
               "marketplace", "country", "base_date", "contract_date",
               "status", "remark"}
    for k, v in (data or {}).items():
        if k in allowed:
            setattr(b, k, v)
    db.commit()
    return to_dict(b)


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    """删除批次：级联删 shipments/items/boxes（模型 cascade）、
    GeneratedDoc 记录 + 磁盘文件、output/{批次名} 目录。"""
    from ..services.generate_service import _safe_name

    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")

    files_removed = 0
    docs = db.query(GeneratedDoc).filter(GeneratedDoc.batch_id == batch_id).all()
    for d in docs:
        if d.path and os.path.exists(d.path):
            try:
                os.remove(d.path)
                files_removed += 1
            except OSError:
                pass  # 文件被占用等情况不阻塞删除
        db.delete(d)

    # 归档目录 output/{批次名}（_safe_name 已滤除路径分隔符，必在 OUTPUT_DIR 内）
    batch_dir = os.path.join(OUTPUT_DIR, _safe_name(b.name or f"batch-{b.id}"))
    if (os.path.isdir(batch_dir)
            and os.path.abspath(batch_dir) != os.path.abspath(OUTPUT_DIR)):
        shutil.rmtree(batch_dir, ignore_errors=True)

    db.delete(b)            # shipments/items/boxes 走模型 cascade
    db.commit()
    return {"deleted": True, "files_removed": files_removed}


@router.get("/batches/{batch_id}/zip")
def batch_zip(batch_id: int, db: Session = Depends(get_db)):
    """打包下载该批次 output 下全部已生成文件（读 GeneratedDoc.path）。"""
    b = db.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    docs = db.query(GeneratedDoc).filter(GeneratedDoc.batch_id == batch_id).all()
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs:
            if d.path and os.path.exists(d.path):
                try:
                    arcname = os.path.relpath(d.path, OUTPUT_DIR)
                except ValueError:
                    arcname = d.filename or os.path.basename(d.path)
                zf.write(d.path, arcname)
                count += 1
    if count == 0:
        raise HTTPException(404, "该批次还没有已生成的文件")
    fname = quote(f"{b.name or b.inbound_plan_id}.zip")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{fname}"})


# ---------------------------------------------------------------- 货件 / 明细编辑

def _edit_with_tracking(obj, data, skip_cols):
    """对比传入值与现值，变化的字段并入 edited_fields。"""
    edited = set(json.loads(obj.edited_fields or "[]"))
    cols = {c.name: c for c in obj.__table__.columns}
    for k, v in (data or {}).items():
        if k in skip_cols or k not in cols:
            continue
        cur = getattr(obj, k)
        # 轻量类型对齐（前端可能传字符串数字）
        if v is not None and cur is not None:
            try:
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, int):
                    v = int(v)
                elif isinstance(cur, float):
                    v = float(v)
            except (TypeError, ValueError):
                pass
        if cur != v:
            setattr(obj, k, v)
            edited.add(k)
    obj.edited_fields = json.dumps(sorted(edited), ensure_ascii=False)
    return sorted(edited)


@router.put("/shipments/{shipment_id}")
def update_shipment(shipment_id: int, data: dict, db: Session = Depends(get_db)):
    sp = db.get(Shipment, shipment_id)
    if sp is None:
        raise HTTPException(404, f"货件 {shipment_id} 不存在")
    edited = _edit_with_tracking(sp, data, {"id", "batch_id", "edited_fields"})
    db.commit()
    d = to_dict(sp)
    d["edited_fields"] = edited
    return d


@router.put("/shipment-items/{item_id}")
def update_shipment_item(item_id: int, data: dict, db: Session = Depends(get_db)):
    it = db.get(ShipmentItem, item_id)
    if it is None:
        raise HTTPException(404, f"明细 {item_id} 不存在")
    edited = _edit_with_tracking(it, data, {"id", "shipment_id", "edited_fields"})
    db.commit()
    d = to_dict(it)
    d["edited_fields"] = edited
    return d
