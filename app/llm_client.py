"""LLM 网关客户端（OpenAI 兼容，jiekou.ai）—— 询价起草/报价提取/回复归属、飞书意图都走这里。

为什么不是 Anthropic SDK：本项目 ANTHROPIC_API_KEY 空着，改用用户提供的 OpenAI 兼容
网关（jiekou.ai），背后模型仍可是 claude-opus-4-8。统一入口 + 优雅降级，仿 sellfox_client 风格。

配置进 .env：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。缺 key 时 available()=False，
chat() 抛 LLMUnavailable，让上层捕获后给"AI 未接通"提示而不崩。
"""

import json
import os

import httpx

DEFAULT_BASE = "https://api.jiekou.ai/openai"
DEFAULT_MODEL = "claude-opus-4-8"


class LLMUnavailable(RuntimeError):
    """LLM 未配置或网关调用失败。"""


def _env(key, default=""):
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


def _key():
    return _env("LLM_API_KEY")


def _base():
    return (_env("LLM_BASE_URL") or DEFAULT_BASE).rstrip("/")


def model():
    return _env("LLM_MODEL") or DEFAULT_MODEL


def available():
    """key 是否就绪——上层据此决定是否提示"AI 未接通"，不直接报错。"""
    return bool(_key())


def chat(messages, *, temperature=0.0, max_tokens=1024, model_name=None, timeout=90,
         response_json=False):
    """调 OpenAI 兼容 /v1/chat/completions，返回 assistant 文本。

    messages: [{"role": "system"|"user"|"assistant", "content": str}]
    response_json: True 时请求 JSON 对象输出（用于结构化提取）。
    缺 key / 网关失败 → LLMUnavailable。
    """
    key = _key()
    if not key:
        raise LLMUnavailable("LLM 未配置（缺 LLM_API_KEY）")
    body = {
        "model": model_name or model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_json:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(f"{_base()}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"LLM 网关请求失败：{e}") from e
    if r.status_code != 200:
        raise LLMUnavailable(f"LLM 网关 HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        raise LLMUnavailable(f"LLM 网关返回异常：{str(data)[:200] if 'data' in dir() else r.text[:200]}") from e


def chat_json(messages, **kw):
    """chat() 的 JSON 便捷封装：请求 JSON 输出并解析成 dict（解析失败返回 {}）。"""
    raw = chat(messages, response_json=True, **kw)
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw[4:].strip() if raw.lower().startswith("json") else raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except (ValueError, TypeError):
                pass
    return {}
