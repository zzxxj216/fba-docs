"""通用 CRUD 路由 + 导入入口。

资源：factories, companies, brands, products, forwarders, templates, rule-configs
- 列表带关联名称冗余字段（brands→factory_name/company_name，templates→forwarder_name）
- DELETE：Factory/Company/Brand 被 Batch/Brand 引用时改为停用，返回 {"deactivated": true}
"""

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Batch, Brand, Company, Factory, Forwarder, GeneratedDoc, Product,
    RuleConfig, Shipment, ShipmentItem, Template,
)
from ..services import import_service

router = APIRouter()

RESOURCES = {
    "factories": Factory,
    "companies": Company,
    "brands": Brand,
    "products": Product,
    "forwarders": Forwarder,
    "templates": Template,
    "rule-configs": RuleConfig,
}


def to_dict(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _model(resource):
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(404, f"未知资源: {resource}")
    return model


def _apply(obj, data):
    """按模型列过滤直传 dict；dict/list 自动 JSON 序列化（如 Template.mapping）。"""
    cols = {c.name for c in obj.__table__.columns} - {"id", "created_at", "updated_at"}
    for k, v in (data or {}).items():
        if k not in cols:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        setattr(obj, k, v)


# ---------------------------------------------------------------- 导入（须在 /{resource} 之前注册）

@router.post("/products/import")
async def products_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        return import_service.import_products(db, content)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/products/import-template")
def products_import_template():
    data = import_service.product_import_template()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%E4%BA%A7%E5%93%81%E5%AF%BC%E5%85%A5%E6%A8%A1%E6%9D%BF.xlsx"},
    )


@router.post("/brands/import")
async def brands_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    return import_service.import_brands(db, content)


# ---------------------------------------------------------------- 通用 CRUD

@router.get("/{resource}")
def list_resource(resource: str, db: Session = Depends(get_db)):
    model = _model(resource)
    rows = db.query(model).order_by(model.id).all()
    out = [to_dict(r) for r in rows]

    if resource == "brands":
        factories = {f.id: f.name for f in db.query(Factory).all()}
        companies = {c.id: c.name_cn for c in db.query(Company).all()}
        for d in out:
            d["factory_name"] = factories.get(d.get("factory_id"), "")
            d["company_name"] = companies.get(d.get("company_id"), "")
    elif resource == "templates":
        forwarders = {f.id: f.name for f in db.query(Forwarder).all()}
        for d in out:
            d["forwarder_name"] = forwarders.get(d.get("forwarder_id"), "")
    return out


@router.post("/{resource}")
def create_resource(resource: str, data: dict, db: Session = Depends(get_db)):
    model = _model(resource)
    obj = model()
    _apply(obj, data)
    db.add(obj)
    db.commit()
    return to_dict(obj)


@router.put("/{resource}/{item_id}")
def update_resource(resource: str, item_id: int, data: dict,
                    db: Session = Depends(get_db)):
    model = _model(resource)
    obj = db.get(model, item_id)
    if obj is None:
        raise HTTPException(404, f"{resource} {item_id} 不存在")
    _apply(obj, data)
    db.commit()
    return to_dict(obj)


# 被引用即只停用的资源：模型 → [(引用模型, 外键列)]
PROTECTED_REFS = {
    Factory: [(Batch, Batch.factory_id), (Brand, Brand.factory_id)],
    Company: [(Batch, Batch.company_id), (Brand, Brand.company_id)],
    Brand: [(Batch, Batch.brand_id)],
}
# 其它有外键引用的资源：被引用则拒绝删除
BLOCKING_REFS = {
    Forwarder: [(Shipment, Shipment.forwarder_id), (Template, Template.forwarder_id)],
    Product: [(ShipmentItem, ShipmentItem.product_id)],
    Template: [(GeneratedDoc, GeneratedDoc.template_id)],
}


def _referenced(db, refs, item_id):
    return any(db.query(ref_model.id).filter(fk == item_id).first()
               for ref_model, fk in refs)


@router.delete("/{resource}/{item_id}")
def delete_resource(resource: str, item_id: int, db: Session = Depends(get_db)):
    model = _model(resource)
    obj = db.get(model, item_id)
    if obj is None:
        raise HTTPException(404, f"{resource} {item_id} 不存在")

    if model in PROTECTED_REFS and _referenced(db, PROTECTED_REFS[model], item_id):
        obj.active = False
        db.commit()
        return {"deactivated": True, "msg": "已被批次/品牌引用，已改为停用"}

    if model in BLOCKING_REFS and _referenced(db, BLOCKING_REFS[model], item_id):
        if hasattr(obj, "active"):
            obj.active = False
            db.commit()
            return {"deactivated": True, "msg": "已被引用，已改为停用"}
        raise HTTPException(400, f"{resource} {item_id} 已被引用，无法删除")

    db.delete(obj)
    db.commit()
    return {"deleted": True}
