"""批次标准 SOP 流程 —— 贯穿建仓→生成文件→标签→发送→采购→进仓跟进的进度状态机。

步骤分两类：
- auto   ：从数据自动判定是否完成（建仓有FC / 已生成文件 / 已生成采购单）
- manual ：人工在进度条上勾选完成，记录在 Batch.sop_done（JSON list）

步骤顺序参考真实 SOP（建仓最先、采购在后、进仓后跟进）。改流程只动这里的 STEPS。
"""

import json

from .models import GeneratedDoc, PurchasePlanConfirm
from .services import batch_prep_service

STEPS = [
    {"key": "prep", "label": "数据准备", "desc": "聚合补仓/建仓清单 + 校验数据齐全", "type": "auto", "action": "prep"},
    {"key": "build", "label": "建仓", "desc": "亚马逊 STA 分仓 · 得目的仓 FC", "type": "auto", "action": "build"},
    {"key": "generate", "label": "生成文件", "desc": "托书/报关/投保/采购合同/9810", "type": "auto", "action": "generate"},
    {"key": "labels", "label": "整理标签", "desc": "外箱标(裁剪) + 货代标", "type": "manual"},
    {"key": "send_factory", "label": "发标签给工厂", "desc": "飞书工厂群：货代标/外箱标/托书", "type": "manual"},
    {"key": "send_forwarder", "label": "发文件给货代", "desc": "托书→报关→投保（微信群）", "type": "manual"},
    {"key": "purchase", "label": "采购", "desc": "ERP 生成采购单/下单/到货", "type": "auto", "action": "purchase"},
    {"key": "logistics_cost", "label": "物流成本录入", "desc": "进仓后按货代数据录入", "type": "manual"},
    {"key": "fee_check", "label": "保费/预录单核对", "desc": "按保险费率核对保费", "type": "manual"},
    {"key": "tracking", "label": "货物状态", "desc": "Tracking ID / Delivery Window", "type": "manual"},
]


def _auto_status(db, batch):
    """auto 类步骤的完成判定。"""
    prep_ready = batch_prep_service.aggregate(db, batch)["ready"]
    has_fc = any((sp.fc_code or "").strip() for sp in batch.shipments)
    doc_count = db.query(GeneratedDoc).filter(GeneratedDoc.batch_id == batch.id).count()
    purchased = False
    if batch.purchase_plan_no:
        rec = (db.query(PurchasePlanConfirm)
               .filter(PurchasePlanConfirm.plan_group_no == batch.purchase_plan_no).first())
        purchased = bool(rec and rec.purchase_no)
    return {"prep": prep_ready, "build": has_fc, "generate": doc_count > 0, "purchase": purchased}


def compute(db, batch):
    """返回 {steps:[{key,label,desc,type,action?,done}], current, done_count, total}。"""
    try:
        done_manual = set(json.loads(batch.sop_done or "[]"))
    except (ValueError, TypeError):
        done_manual = set()
    auto = _auto_status(db, batch)
    steps = []
    for s in STEPS:
        done = auto.get(s["key"], False) if s["type"] == "auto" else (s["key"] in done_manual)
        steps.append({**s, "done": done})
    current = next((s["key"] for s in steps if not s["done"]), None)
    return {"steps": steps, "current": current,
            "done_count": sum(1 for s in steps if s["done"]), "total": len(steps)}


def toggle(db, batch, step_key, done):
    """人工勾选/取消一个 manual 步骤；auto 步骤不可手动改。返回新的 compute()。"""
    meta = next((s for s in STEPS if s["key"] == step_key), None)
    if meta is None:
        raise RuntimeError(f"未知步骤 {step_key}")
    if meta["type"] != "manual":
        raise RuntimeError(f"步骤「{meta['label']}」由数据自动判定，不能手动勾选")
    try:
        cur = set(json.loads(batch.sop_done or "[]"))
    except (ValueError, TypeError):
        cur = set()
    if done:
        cur.add(step_key)
    else:
        cur.discard(step_key)
    batch.sop_done = json.dumps(sorted(cur), ensure_ascii=False)
    db.commit()
    return compute(db, batch)
