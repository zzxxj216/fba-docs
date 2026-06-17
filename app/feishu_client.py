"""飞书机器人客户端（lark-oapi 封装）—— 内部运营入口（Track-B）。

职责（仿 qiwe_client 风格，优雅降级）：
- tenant_access_token 由 lark-oapi Client 内部管理（传 app_id/secret 即可）。
- 发文本 send_text / 发交互卡片 send_card（消息卡片，带按钮）/ 回复消息 reply_text。
- configured()：缺 FEISHU_APP_ID/SECRET 时为 False，上层据此提示"飞书未接通"而不崩。

凭据进 .env（勿提交）：FEISHU_APP_ID / FEISHU_APP_SECRET。

收消息/按钮回调不在这里：走长连接 lark.ws.Client（见 feishu/handlers.py），
本模块只管"主动外呼"（发消息/发卡片）与 client 单例。
"""

import json
import os

_client = None


def _env(key, default=""):
    """读环境变量；.env 带 BOM 时兜底直读（与 qiwe_client/llm_client 一致）。"""
    v = os.getenv(key)
    if v:
        return v
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, val = line.partition("=")
                    if k.strip().lstrip("﻿") == key:
                        return val.strip().strip('"').strip("'")
    return default


def app_id():
    return _env("FEISHU_APP_ID")


def app_secret():
    return _env("FEISHU_APP_SECRET")


def configured():
    """凭据是否就绪——上层据此决定是否提示"飞书未接通"，避免直接报错。"""
    return bool(app_id() and app_secret())


def get_client():
    """lark-oapi Client 单例（内部自动管 tenant_access_token）。

    未配置 → RuntimeError（上层捕获给"未接通"提示）。
    """
    global _client
    if not configured():
        raise RuntimeError("飞书未配置（缺 FEISHU_APP_ID / FEISHU_APP_SECRET）")
    if _client is None:
        import lark_oapi as lark
        _client = (lark.Client.builder()
                   .app_id(app_id())
                   .app_secret(app_secret())
                   .log_level(lark.LogLevel.WARNING)
                   .build())
    return _client


def _send(receive_id, receive_id_type, msg_type, content_obj):
    """统一发消息。content_obj 为 dict，按飞书要求 json 序列化成 content 字符串。

    返回飞书 message_id（失败抛 RuntimeError）。
    """
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest, CreateMessageRequestBody,
    )
    client = get_client()
    body = (CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(json.dumps(content_obj, ensure_ascii=False))
            .build())
    req = (CreateMessageRequest.builder()
           .receive_id_type(receive_id_type)
           .request_body(body)
           .build())
    resp = client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(f"飞书发消息失败 code={resp.code}: {resp.msg}")
    return resp.data.message_id if resp.data else None


def send_text(receive_id, text, receive_id_type="chat_id"):
    """发纯文本。receive_id_type: chat_id / open_id / user_id / union_id / email。"""
    return _send(receive_id, receive_id_type, "text", {"text": text})


def send_card(receive_id, card, receive_id_type="chat_id"):
    """发交互卡片（消息卡片，可带按钮）。card 为卡片 JSON（dict，见 feishu/cards.py）。"""
    return _send(receive_id, receive_id_type, "interactive", card)


def reply_text(message_id, text):
    """回复某条消息（thread 内）。"""
    return _reply(message_id, "text", {"text": text})


def reply_card(message_id, card):
    """以卡片回复某条消息。"""
    return _reply(message_id, "interactive", card)


def _reply(message_id, msg_type, content_obj):
    from lark_oapi.api.im.v1 import (
        ReplyMessageRequest, ReplyMessageRequestBody,
    )
    client = get_client()
    body = (ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(json.dumps(content_obj, ensure_ascii=False))
            .build())
    req = (ReplyMessageRequest.builder()
           .message_id(message_id)
           .request_body(body)
           .build())
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        raise RuntimeError(f"飞书回复消息失败 code={resp.code}: {resp.msg}")
    return resp.data.message_id if resp.data else None
