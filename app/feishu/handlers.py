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

import collections
import json
import threading

from .. import feishu_client as fc
from ..database import SessionLocal
from ..logging_config import get_logger
from . import service

logger = get_logger(__name__)

_ws_thread = None

# 去重：飞书长连接"至少投递一次"，处理慢会触发重推。按 message_id/事件键记已处理，
# 重推的同一条直接跳过，避免机器人对一条消息反复回复（"自动推送"刷屏的根因）。
_SEEN = collections.OrderedDict()
_SEEN_LOCK = threading.Lock()


def _seen_once(key):
    """首次见到返回 False 并记录；已见过返回 True（应跳过）。容量上限自动淘汰。"""
    if not key:
        return False
    with _SEEN_LOCK:
        if key in _SEEN:
            return True
        _SEEN[key] = 1
        while len(_SEEN) > 2000:
            _SEEN.popitem(last=False)
    return False


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


def _process_message(chat_id, open_id, user_id, text):
    """后台线程里真正处理（含 LLM/发送），不阻塞长连接 ACK。"""
    db = SessionLocal()
    try:
        service.handle_message(db, chat_id=chat_id, open_id=open_id,
                               user_id=user_id, text=text)
    except Exception:
        logger.exception("handle_message failed")
    finally:
        db.close()


def on_message(data):
    """im.message.receive_v1：去重 + 立即返回，重活丢后台线程（避免飞书重推刷屏）。"""
    try:
        event = data.event
        message = event.message
        sender = event.sender
        msg_id = getattr(message, "message_id", None)
        chat_id = message.chat_id
        open_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
        user_id = getattr(getattr(sender, "sender_id", None), "user_id", None)
        text = _extract_text(message)
    except Exception:
        return
    if _seen_once(msg_id):                      # 飞书重推的同一条 → 跳过
        logger.info("dup message skipped msg_id=%s", msg_id)
        return
    logger.info("recv msg_id=%s open_id=%s text=%r", msg_id, open_id, text)
    threading.Thread(target=_process_message, name="feishu-msg",
                     args=(chat_id, open_id, user_id, text), daemon=True).start()


def _process_action(chat_id, open_id, act, value):
    """后台线程里执行按钮动作（含询价线调用/发卡片），不阻塞回调响应。"""
    db = SessionLocal()
    try:
        service.handle_action(db, chat_id=chat_id, open_id=open_id,
                              action=act, value=value)
    except Exception:
        logger.exception("handle_action failed")
    finally:
        db.close()


def on_card_action(data):
    """card.action.trigger：去重 + 立即回 toast，动作丢后台线程。"""
    try:
        event = data.event
        action = event.action
        value = action.value or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                value = {}
        act = value.get("action", "")
        operator = getattr(event, "operator", None)
        open_id = getattr(operator, "open_id", None)
        ctx = getattr(event, "context", None)
        chat_id = getattr(ctx, "open_chat_id", None) if ctx else None
        # 去重键：飞书回调有 token；没有就用 open_id+动作+批次/询价 兜底
        token = getattr(event, "token", None)
        key = "card:" + (token or f"{open_id}:{act}:{value.get('batch_id')}:{value.get('inquiry_id')}")
    except Exception:
        return _toast("处理失败")
    if _seen_once(key):
        return _toast("已处理")
    logger.info("card action=%s open_id=%s", act, open_id)
    threading.Thread(target=_process_action, name="feishu-card",
                     args=(chat_id, open_id, act, value), daemon=True).start()
    return _toast("已收到，处理中…")


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
