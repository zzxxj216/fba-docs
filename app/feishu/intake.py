"""初始化流程（intake）—— 运营在飞书唤醒后的第一步。

唤醒 → 认人（Operator 查管哪些店铺/品牌）→ 建/复用 Session + 工作文件夹 →
拉该运营店铺的待采购/待建仓批次 → 复用 batch_prep_service 做"建仓就绪体检" →
组装成待办数据（service 据此回待办卡片）。

不发网络、不直接发卡片——纯数据组装，方便单测（mock db）。
"""

import json
import os

from ..database import OUTPUT_DIR
from ..feishu_models import FeishuSession, Operator
from ..models import Batch, Brand
from ..services import batch_prep_service as prep

# 视为"待办"的批次状态：尚未生成完文件、可能要建仓/询价的。
PENDING_STATUSES = ("已同步", "已核对", "待投保")


def identify_operator(db, open_id=None, user_id=None):
    """认人：按 open_id / user_id 找 Operator。找不到返回 None（service 提示登记）。"""
    q = db.query(Operator).filter(Operator.active.is_(True))
    if open_id:
        op = q.filter(Operator.feishu_open_id == open_id).first()
        if op:
            return op
    if user_id:
        op = q.filter(Operator.feishu_user_id == user_id).first()
        if op:
            return op
    return None


def _scope(op):
    """解析运营管辖范围。返回 (brand_ids:set|None, shop_names:set|None)；None=不限。"""
    if op is None or op.is_admin:
        return None, None
    brand_ids = None
    shop_names = None
    if op.scope_brand_ids:
        try:
            brand_ids = set(json.loads(op.scope_brand_ids) or [])
        except (ValueError, TypeError):
            brand_ids = None
    if op.scope_shop_names:
        try:
            shop_names = {s.strip().lower() for s in (json.loads(op.scope_shop_names) or [])}
        except (ValueError, TypeError):
            shop_names = None
    # 两个都空 = 没设范围 = 看不到（保守），除非 admin
    if brand_ids is None and shop_names is None:
        return set(), set()
    return brand_ids, shop_names


def list_operator_batches(db, op):
    """该运营管辖范围内的待办批次（按管辖品牌/店铺过滤 + 状态过滤）。"""
    brand_ids, shop_names = _scope(op)
    q = db.query(Batch).filter(Batch.status.in_(PENDING_STATUSES))
    rows = q.order_by(Batch.id.desc()).all()
    out = []
    for b in rows:
        if brand_ids is None and shop_names is None:
            out.append(b)                       # admin / 不限
            continue
        hit = False
        if brand_ids and b.brand_id in brand_ids:
            hit = True
        if not hit and shop_names and (b.shop_name or "").strip().lower() in shop_names:
            hit = True
        if hit:
            out.append(b)
    return out


def health_check(db, batch):
    """对单批次跑建仓就绪体检（复用 batch_prep_service.aggregate）。

    返回待办卡片需要的精简字段。
    """
    agg = prep.aggregate(db, batch)
    brand = db.get(Brand, batch.brand_id) if batch.brand_id else None
    return {
        "batch_id": batch.id,
        "name": batch.name,
        "brand": brand.name if brand else "",
        "shop_name": batch.shop_name,
        "sku_count": agg.get("sku_count", 0),
        "total_qty": agg.get("total_qty", 0),
        "ready": agg.get("ready", False),
        "issues": agg.get("issues", []),
        "issue_count": len(agg.get("issues", [])),
    }


def ensure_session(db, op, chat_id, open_id):
    """建/复用本会话 + 建工作文件夹。返回 FeishuSession。"""
    sess = (db.query(FeishuSession)
            .filter(FeishuSession.chat_id == chat_id,
                    FeishuSession.status == "active")
            .order_by(FeishuSession.id.desc()).first())
    if sess is None:
        sess = FeishuSession(operator_id=op.id if op else None,
                             chat_id=chat_id, open_id=open_id, status="active")
        db.add(sess)
        db.commit()
        db.refresh(sess)
    work_dir = _work_dir(sess)
    if sess.work_dir != work_dir:
        sess.work_dir = work_dir
        db.commit()
    try:
        os.makedirs(work_dir, exist_ok=True)
    except OSError:
        pass
    return sess


def _work_dir(sess):
    return os.path.join(OUTPUT_DIR, "_feishu", f"session_{sess.id}")


def build_intake(db, op, chat_id, open_id):
    """完整 intake：建会话 + 体检管辖批次 → {session, operator, todos}。"""
    sess = ensure_session(db, op, chat_id, open_id)
    batches = list_operator_batches(db, op)
    todos = [health_check(db, b) for b in batches]
    return {"session": sess, "operator": op, "todos": todos}
