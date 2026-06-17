"""长连接事件分发单测 —— 用假事件对象验证文本提取/参数解析；不真连飞书。"""

import json
import types

from app.feishu import handlers


def _msg(message_type, content):
    return types.SimpleNamespace(message_type=message_type, content=content)


def test_extract_text_plain():
    m = _msg("text", json.dumps({"text": "你好待办"}))
    assert handlers._extract_text(m) == "你好待办"


def test_extract_text_post():
    content = json.dumps({"content": [[{"tag": "text", "text": "建仓"},
                                       {"tag": "a", "text": "x"}]]})
    m = _msg("post", content)
    assert handlers._extract_text(m) == "建仓"


def test_extract_text_bad_json():
    assert handlers._extract_text(_msg("text", "not-json")) == ""
    assert handlers._extract_text(_msg("image", "{}")) == ""


def test_on_message_routes_to_service(monkeypatch):
    captured = {}

    def fake_handle(db, **kw):
        captured.update(kw)

    monkeypatch.setattr(handlers.service, "handle_message", fake_handle)
    # 假 SessionLocal，避免连库
    monkeypatch.setattr(handlers, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))

    sender_id = types.SimpleNamespace(open_id="ou_x", user_id="u1")
    event = types.SimpleNamespace(
        message=types.SimpleNamespace(message_type="text",
                                      content=json.dumps({"text": "待办"}),
                                      chat_id="chat1"),
        sender=types.SimpleNamespace(sender_id=sender_id))
    data = types.SimpleNamespace(event=event)
    handlers.on_message(data)
    assert captured["chat_id"] == "chat1"
    assert captured["open_id"] == "ou_x"
    assert captured["text"] == "待办"


def test_on_card_action_routes_and_returns_toast(monkeypatch):
    captured = {}

    def fake_action(db, **kw):
        captured.update(kw)

    monkeypatch.setattr(handlers.service, "handle_action", fake_action)
    monkeypatch.setattr(handlers, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))

    event = types.SimpleNamespace(
        action=types.SimpleNamespace(value={"action": "start_inquiry", "batch_id": 7}),
        operator=types.SimpleNamespace(open_id="ou_y"),
        context=types.SimpleNamespace(open_chat_id="chat9"))
    data = types.SimpleNamespace(event=event)
    resp = handlers.on_card_action(data)
    assert resp["toast"]["content"] == "已处理"
    assert captured["action"] == "start_inquiry"
    assert captured["value"]["batch_id"] == 7
    assert captured["chat_id"] == "chat9"


def test_build_ws_client_requires_creds(monkeypatch):
    monkeypatch.setattr(handlers.fc, "configured", lambda: False)
    import pytest
    with pytest.raises(RuntimeError):
        handlers.build_ws_client()
