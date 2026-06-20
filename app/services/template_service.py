"""模板分层解析：按"具体度"为 (店铺品牌 / 主体 / 货代 / doc_type) 选最合适的模板。

具体度打分：店铺 brand_id 匹配 +100；主体 company_id 匹配 +10；货代 forwarder_id 匹配 +10；
全空=全局兜底(0)。任一 scope 设了但不匹配 → 该模板不适用(淘汰)。

→ 托书 master 设 forwarder_id；报关/投保/采购合同 master 设 company_id；
  某店铺特殊则建一份设 brand_id 的 override，自动最优先。
"""

from ..models import Template


def _score(t, brand_id, company_id, forwarder_id):
    """模板对给定场景的具体度分；任一已设 scope 不匹配返回 None（不适用）。"""
    s = 0
    if t.brand_id:
        if t.brand_id != brand_id:
            return None
        s += 100
    if t.company_id:
        if t.company_id != company_id:
            return None
        s += 10
    if t.forwarder_id:
        if t.forwarder_id != forwarder_id:
            return None
        s += 10
    return s


def resolve_template(db, doc_type, *, brand=None, forwarder=None, company=None,
                     brand_id=None, company_id=None, forwarder_id=None):
    """为某 doc_type 选最具体的有效模板。返回 Template 或 None。"""
    bid = brand_id if brand_id is not None else (brand.id if brand else None)
    cid = company_id if company_id is not None else (
        company.id if company else (brand.company_id if brand else None))
    fid = forwarder_id if forwarder_id is not None else (forwarder.id if forwarder else None)
    best, best_s = None, -1
    for t in (db.query(Template)
              .filter(Template.doc_type == doc_type, Template.active.is_(True)).all()):
        s = _score(t, bid, cid, fid)
        if s is not None and s > best_s:
            best, best_s = t, s
    return best


def resolve_templates(db, doc_types, *, brand=None, forwarder=None, company=None):
    """批量解析多个 doc_type → {doc_type: Template|None}。"""
    return {dt: resolve_template(db, dt, brand=brand, forwarder=forwarder, company=company)
            for dt in doc_types}


def list_by_scope(db):
    """模板库分层视图：店铺override / 主体master / 货代master / 全局。"""
    out = {"brand": [], "company": [], "forwarder": [], "global": []}
    for t in (db.query(Template).filter(Template.active.is_(True))
              .order_by(Template.doc_type, Template.id).all()):
        info = {"id": t.id, "name": t.name, "doc_type": t.doc_type,
                "forwarder_id": t.forwarder_id, "company_id": t.company_id,
                "brand_id": t.brand_id, "granularity": t.granularity}
        if t.brand_id:
            out["brand"].append(info)
        elif t.company_id:
            out["company"].append(info)
        elif t.forwarder_id:
            out["forwarder"].append(info)
        else:
            out["global"].append(info)
    return out
