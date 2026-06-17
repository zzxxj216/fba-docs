"""飞书线路由：状态/接通 + 运营/会话管理 + 调试入口（手动触发 intake/动作）。

飞书的真实收发走长连接（handlers.start_background），不靠 HTTP 回调；这里的路由
主要给前端/集成用：查接通状态、登记运营、手动测试 intake 与按钮动作。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import feishu_client as fc
from ..feishu import inquiry_port as port
from ..feishu import service
from ..feishu_models import Operator

router = APIRouter()


@router.get("/feishu/status")
def feishu_status():
    """是否接通：凭据是否就绪 + LLM 是否可用 + 询价线是否已接入（否则走 stub）。"""
    from .. import llm_client
    return {
        "feishu_configured": fc.configured(),
        "llm_available": llm_client.available(),
        "inquiry_stub": port.using_stub(),
    }


@router.post("/feishu/ws/start")
def feishu_ws_start():
    """后台启动长连接（连上飞书）。缺凭据 400。"""
    from ..feishu import handlers
    if not fc.configured():
        raise HTTPException(400, "飞书未配置（缺 FEISHU_APP_ID / FEISHU_APP_SECRET）")
    try:
        t = handlers.start_background()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"started": True, "thread_alive": t.is_alive()}


# ---------------------------------------------------------------- 运营管理

@router.get("/feishu/operators")
def list_operators(db: Session = Depends(get_db)):
    rows = db.query(Operator).order_by(Operator.id.desc()).all()
    return [{"id": o.id, "name": o.name, "feishu_open_id": o.feishu_open_id,
             "feishu_user_id": o.feishu_user_id, "is_admin": o.is_admin,
             "scope_brand_ids": o.scope_brand_ids, "scope_shop_names": o.scope_shop_names,
             "active": o.active} for o in rows]


@router.post("/feishu/operators")
def create_operator(data: dict, db: Session = Depends(get_db)):
    """登记运营。body: {name, feishu_open_id, feishu_user_id?, is_admin?,
    scope_brand_ids?(JSON str/list), scope_shop_names?}。"""
    data = data or {}
    cols = {c.name for c in Operator.__table__.columns}
    payload = {k: v for k, v in data.items() if k in cols}
    if not payload.get("name"):
        raise HTTPException(400, "name 不能为空")
    op = Operator(**payload)
    db.add(op)
    db.commit()
    db.refresh(op)
    return {"id": op.id}


# ---------------------------------------------------------------- 调试：模拟消息/动作

@router.post("/feishu/simulate/message")
def simulate_message(data: dict, db: Session = Depends(get_db)):
    """不经飞书，直接走一遍 handle_message（集成/联调用）。

    body: {chat_id, open_id?, user_id?, text}。返回组装出的卡片（不一定真发出）。
    """
    data = data or {}
    res = service.handle_message(
        db, chat_id=data.get("chat_id", "test_chat"),
        open_id=data.get("open_id"), user_id=data.get("user_id"),
        text=data.get("text", ""))
    return {"card": res.get("card"), "sent": bool(res.get("sent")),
            "intent": res.get("intent"), "todos": res.get("todos")}


@router.post("/feishu/simulate/action")
def simulate_action(data: dict, db: Session = Depends(get_db)):
    """不经飞书，直接走一遍 handle_action。

    body: {chat_id?, open_id?, action, value(dict)}。
    """
    data = data or {}
    if not data.get("action"):
        raise HTTPException(400, "action 不能为空")
    res = service.handle_action(
        db, chat_id=data.get("chat_id", "test_chat"),
        open_id=data.get("open_id"), action=data["action"],
        value=data.get("value") or {})
    return {"card": res.get("card"), "sent": bool(res.get("sent")),
            "action": res.get("action")}
