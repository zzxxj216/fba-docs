"""意图解析单测 —— mock llm_client，覆盖 LLM 命中 / LLM 不可用关键词兜底。"""

from app import llm_client
from app.feishu import service


def test_keyword_fallback_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    assert service.parse_intent("帮我看看待办")["intent"] == "intake"
    assert service.parse_intent("给这批货发个询价")["intent"] == "inquiry"
    assert service.parse_intent("哪家货代便宜")["intent"] == "compare"
    assert service.parse_intent("帮我补数据")["intent"] == "fill_data"
    assert service.parse_intent("help")["intent"] == "help"


def test_empty_text_defaults_intake(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    assert service.parse_intent("")["intent"] == "intake"


def test_unknown_keyword_defaults_intake(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    assert service.parse_intent("早上好")["intent"] == "intake"


def test_llm_intent_used_when_available(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: True)
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **k: {"intent": "compare", "batch_hint": "Serenorch-US-0619"})
    res = service.parse_intent("随便一句")
    assert res["intent"] == "compare"
    assert res["batch_hint"] == "Serenorch-US-0619"


def test_llm_failure_falls_back_to_keyword(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: True)

    def boom(*a, **k):
        raise llm_client.LLMUnavailable("网关挂了")

    monkeypatch.setattr(llm_client, "chat_json", boom)
    # LLM 抛错 → 关键词兜底
    assert service.parse_intent("发询价给货代")["intent"] == "inquiry"


def test_llm_invalid_intent_falls_back(monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: True)
    monkeypatch.setattr(llm_client, "chat_json", lambda *a, **k: {"intent": "garbage"})
    # 非法 intent → 关键词兜底（这里命中 inquiry）
    assert service.parse_intent("询价")["intent"] == "inquiry"
