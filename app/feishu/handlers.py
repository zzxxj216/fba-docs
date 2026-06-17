"""飞书长连接事件接入（lark.ws.Client，免公网）。

接收两类事件并分发到 service：
1. im.message.receive_v1   运营给机器人发消息（文本）→ service.handle_message
2. card.action.trigger     运营点卡片按钮 → service.handle_action

用法：
    from app.feishu import handlers
    handlers.start()          # 阻塞，建议放后台线程/独立进程
    cli = handlers.build_ws_client()   # 仅构造不连接（测试/集成用）

缺凭据时 build_ws_client / start 抛 RuntimeError（feishu_client.configured()=False），
路由层据此报"未接通"，不崩。
"""

import json
import threading

from .. import feishu_client as fc
from ..database import SessionLocal
from . import service

_ws_thread = None


# ---------------------------------------------------------------- 事件回调

def _extract_text(message):
    """从 im 消息事件里取纯文本（content 是 JSON 串，text 类型为 {"text": "..."}）。"""
    try:
        content = json.loads(message.content or "{}")
    except (ValueError, TypeError):
        return ""
    if message.message_type == "text":
        return (content.get("text") or "").strip()
    # 富文本/post 简单兜底：拼所有文本片段
    if message.message_type == "post":
        out = []
        for blocks in (content.get("content") or []):
            for el in blocks:
                if isinstance(el, dict) and el.get("tag") == "text":
                    out.append(el.get("text", ""))
        return "".join(out).strip()
    return ""


def on_message(data):
    """im.message.receive_v1 处理：认人 + intake → 回待办卡片。机器人自身消息忽略。"""
    try:
        event = data.event
        message = event.message
        sender = event.sender
        chat_id = message.chat_id
        open_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
        user_id = getattr(getattr(sender, "sender_id", None), "user_id", None)
        text = _extract_text(message)
    except Exception:
        return
    db = SessionLocal()
    try:
        service.handle_message(db, chat_id=chat_id, open_id=open_id,
                               user_id=user_id, text=text)
    except Exception:
        pass
    finally:
        db.close()


def on_card_action(data):
    """card.action.trigger 处理：按钮 value → service.handle_action。

    需返回一个 toast/卡片更新响应；这里返回轻提示，真正的下一步卡片由 service 另发。
    """
    try:
        event = data.event
        action = event.action
        value = action.value or {}
        # value 可能是 dict 或 JSON 串
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                value = {}
        act = value.get("action", "")
        operator = getattr(event, "operator", None)
        open_id = getattr(operator, "open_id", None)
        # card.action 事件里 chat 信息在 context
        ctx = getattr(event, "context", None)
        chat_id = getattr(ctx, "open_chat_id", None) if ctx else None
    except Exception:
        return _toast("处理失败")
    db = SessionLocal()
    try:
        service.handle_action(db, chat_id=chat_id, open_id=open_id,
                              action=act, value=value)
    except Exception:
        return _toast("处理失败")
    finally:
        db.close()
    return _toast("已处理")


def _toast(text):
    """卡片回调的即时 toast 响应（飞书要求的返回结构）。"""
    return {"toast": {"type": "info", "content": text}}


# ---------------------------------------------------------------- 长连接

def build_ws_client():
    """构造（不一定连接）lark.ws.Client + 事件处理器。缺凭据抛 RuntimeError。"""
    if not fc.configured():
        raise RuntimeError("飞书未配置（缺 FEISHU_APP_ID / FEISHU_APP_SECRET）")
    import lark_oapi as lark

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card_action)
               .build())
    client = lark.ws.Client(fc.app_id(), fc.app_secret(),
                            event_handler=handler,
                            log_level=lark.LogLevel.WARNING)
    return client


def start():
    """阻塞启动长连接（会一直跑）。建议外层放线程/进程。"""
    client = build_ws_client()
    client.start()


def start_background():
    """后台线程启动长连接，返回线程对象。重复调用复用同一线程。"""
    global _ws_thread
    if _ws_thread and _ws_thread.is_alive():
        return _ws_thread
    if not fc.configured():
        raise RuntimeError("飞书未配置（缺 FEISHU_APP_ID / FEISHU_APP_SECRET）")
    _ws_thread = threading.Thread(target=start, name="feishu-ws", daemon=True)
    _ws_thread.start()
    return _ws_thread
