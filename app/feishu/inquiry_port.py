"""与询价线（Track-A）的接缝适配层。

CONTRACT_PARALLEL.md §2 约定：询价线在 `app/services/inquiry_service.py` 暴露

    start_inquiry(db, batch_id) -> Inquiry
    send_inquiry(db, inquiry_id) -> dict
    get_comparison(db, inquiry_id) -> dict
    choose_forwarder(db, inquiry_id, quote_id)

飞书线按这些签名对接。**询价线可能还没实现**——本模块在 import 失败时退化为
本地 stub（用假数据 + 在 Inquiry 表里留痕），让飞书线能独立完成与测试。
集成时把 `_real()` 能 import 到真实 inquiry_service，自动切换、无需改飞书代码。

切换方式：什么都不用改。只要 `app.services.inquiry_service` 存在且实现了对应函数，
本模块就调真的；否则走 stub。也可设环境变量 FEISHU_INQUIRY_STUB=1 强制 stub（测试用）。
"""

import os


def _real():
    """尝试拿到真实 inquiry_service（询价线）。强制 stub 或未就绪时返回 None。"""
    if os.getenv("FEISHU_INQUIRY_STUB") == "1":
        return None
    try:
        from ..services import inquiry_service as svc
    except Exception:
        return None
    return svc


def using_stub():
    return _real() is None


# ---------------------------------------------------------------- 接缝四函数

def start_inquiry(db, batch_id):
    """建询价 + LLM 起草正文（待人工确认）。返回 Inquiry（或 stub 字典-like）。"""
    svc = _real()
    if svc is not None:
        return svc.start_inquiry(db, batch_id)
    return _stub_start_inquiry(db, batch_id)


def send_inquiry(db, inquiry_id):
    """群发各货代（企微 qiwe）。返回 dict。"""
    svc = _real()
    if svc is not None:
        return svc.send_inquiry(db, inquiry_id)
    return _stub_send_inquiry(db, inquiry_id)


def get_comparison(db, inquiry_id):
    """各货代整包总价 + 推荐。返回 dict。"""
    svc = _real()
    if svc is not None:
        return svc.get_comparison(db, inquiry_id)
    return _stub_get_comparison(db, inquiry_id)


def choose_forwarder(db, inquiry_id, quote_id):
    """人工选定货代。"""
    svc = _real()
    if svc is not None:
        return svc.choose_forwarder(db, inquiry_id, quote_id)
    return _stub_choose_forwarder(db, inquiry_id, quote_id)


# ---------------------------------------------------------------- 本地 stub
# 用真实的 Inquiry 表留痕（表在共享 models 里，已建），比价/报价用假数据。

def _stub_start_inquiry(db, batch_id):
    from ..models import Inquiry
    inq = Inquiry(
        batch_id=batch_id, status="待发送",
        content="【询价线未接入·示例草稿】贵司好，我司一批货需头程报价，"
                "明细见附件，请报整包价/时效/截关，谢谢。",
    )
    db.add(inq)
    db.commit()
    db.refresh(inq)
    return inq


def _stub_send_inquiry(db, inquiry_id):
    from ..models import Inquiry
    inq = db.get(Inquiry, inquiry_id)
    if inq is not None:
        inq.status = "已发送"
        db.commit()
    return {"sent": True, "inquiry_id": inquiry_id, "stub": True,
            "note": "询价线未接入：未真正群发，仅置为已发送（占位）"}


def _stub_get_comparison(db, inquiry_id):
    return {
        "inquiry_id": inquiry_id,
        "recommended_quote_id": 9001,
        "reason": "【示例】总价最低且全仓覆盖（询价线接入后为真实比价）",
        "stub": True,
        "quotes": [
            {"quote_id": 9001, "forwarder_name": "示例货代A", "total_price": 12800,
             "currency": "CNY", "eta_days": 12, "channel": "海运", "risk": "",
             "missing_fc": []},
            {"quote_id": 9002, "forwarder_name": "示例货代B", "total_price": 13500,
             "currency": "CNY", "eta_days": 9, "channel": "空运", "risk": "旺季可能涨价",
             "missing_fc": ["ONT8"]},
        ],
    }


def _stub_choose_forwarder(db, inquiry_id, quote_id):
    from ..models import Inquiry
    inq = db.get(Inquiry, inquiry_id)
    if inq is not None:
        inq.status = "已选货代"
        inq.chosen_quote_id = quote_id
        db.commit()
    return {"chosen": True, "inquiry_id": inquiry_id, "quote_id": quote_id,
            "stub": True}
