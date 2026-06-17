"""LLM 兜底报价提取 + 图片报价（视觉）—— 正则弱时才走，输出同一套 schema。

设计见 AGENT_FORWARDER.md §4。文字与图片用同一 schema（QuoteLine[]），区别只是输入：
  - 文字：正则置信度低 → 把原文喂 LLM 结构化。
  - 图片：货代发价格表截图 → 多模态消息（image_url/base64）喂视觉模型。

一律走 app/llm_client.py（OpenAI 兼容网关，背后 claude-opus-4-8），缺 key 抛 LLMUnavailable，
上层捕获后降级（保留正则结果或标"AI 未接通"）。
"""

import base64
import json
import os

from .. import llm_client

SCHEMA_HINT = (
    "请把货代报价提取成 JSON，结构严格如下：\n"
    '{"currency":"CNY|USD","customs_fee_monthly":数字或null,'
    '"lines":[{"fc":"目的仓代码如ONT8，一口价填ALL","price":运费单价数字,'
    '"currency":"CNY|USD","unit":"/kg|/cbm|/票","channel":"空运|海运|快递等",'
    '"eta_days":整数或null,"cutoff":"截关字符串","min_charge":起送费数字或null,'
    '"remark":"备注"}],"confidence":0到1之间的小数}\n'
    "规则：仓代码常以数字结尾（ONT8），别把代码尾数、时效天数、起送费当成运费；"
    "逐仓多行就拆多行，一口价就一行 fc=ALL；不确定的字段填 null。只输出 JSON。"
)


def _normalize(data, source_type):
    """把 LLM 返回的 dict 归一成与 quote_extractor.extract 一致的结构。"""
    data = data or {}
    raw_lines = data.get("lines") or []
    lines = []
    for ln in raw_lines:
        if not isinstance(ln, dict):
            continue
        price = ln.get("price")
        try:
            price = float(price) if price is not None else None
        except (ValueError, TypeError):
            price = None
        eta = ln.get("eta_days")
        try:
            eta = int(eta) if eta not in (None, "") else None
        except (ValueError, TypeError):
            eta = None
        mc = ln.get("min_charge")
        try:
            mc = float(mc) if mc not in (None, "") else None
        except (ValueError, TypeError):
            mc = None
        lines.append({
            "fc": str(ln.get("fc") or "").upper(),
            "price": price,
            "currency": ln.get("currency") or data.get("currency") or "CNY",
            "unit": ln.get("unit") or "/kg",
            "channel": ln.get("channel") or "",
            "eta_days": eta,
            "cutoff": ln.get("cutoff") or "",
            "min_charge": mc,
            "remark": ln.get("remark") or "",
        })
    conf = data.get("confidence")
    try:
        conf = float(conf)
    except (ValueError, TypeError):
        conf = 0.6 if lines else 0.0
    cfm = data.get("customs_fee_monthly")
    try:
        cfm = float(cfm) if cfm not in (None, "") else None
    except (ValueError, TypeError):
        cfm = None
    return {
        "source_type": source_type,
        "currency": data.get("currency") or "CNY",
        "customs_fee_monthly": cfm,
        "lines": lines,
        "confidence": round(min(max(conf, 0.0), 1.0), 3),
        "notes": ["llm"],
    }


def extract_text(text, lanes=None):
    """文字报价 LLM 兜底。缺 key 抛 LLMUnavailable。"""
    lanes_hint = ""
    if lanes:
        fcs = [str(l.get("fc")) for l in lanes if l.get("fc")]
        if fcs:
            lanes_hint = f"\n本批次目的仓应有：{', '.join(fcs)}（逐仓核对是否报全）。"
    messages = [
        {"role": "system", "content": "你是货代报价提取助手，只输出 JSON。"},
        {"role": "user", "content": SCHEMA_HINT + lanes_hint +
            "\n\n报价原文：\n" + (text or "")},
    ]
    data = llm_client.chat_json(messages, max_tokens=2048)
    return _normalize(data, "text")


def _image_to_data_url(image_path):
    """本地图片 → data URL（base64），用于 OpenAI 兼容多模态 image_url。"""
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def extract_image(image_path, lanes=None):
    """图片报价（视觉）：价格表截图 → QuoteLine[]。

    用 OpenAI 兼容多模态 message（content 为列表，含 text + image_url）。
    缺 key 抛 LLMUnavailable；图片不存在抛 FileNotFoundError。
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    data_url = _image_to_data_url(image_path)
    lanes_hint = ""
    if lanes:
        fcs = [str(l.get("fc")) for l in lanes if l.get("fc")]
        if fcs:
            lanes_hint = f"\n本批次目的仓应有：{', '.join(fcs)}。"
    messages = [
        {"role": "system", "content": "你是货代报价提取助手，看图提取价格表，只输出 JSON。"},
        {"role": "user", "content": [
            {"type": "text", "text": SCHEMA_HINT + lanes_hint +
                "\n\n这是货代发来的价格表截图，逐行提取："},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]
    data = llm_client.chat_json(messages, max_tokens=2048)
    return _normalize(data, "image")


def extract_image_data_url(data_url, lanes=None):
    """已是 data URL / 远程 URL 的图片直接提取（不落本地文件时用）。"""
    messages = [
        {"role": "system", "content": "你是货代报价提取助手，看图提取价格表，只输出 JSON。"},
        {"role": "user", "content": [
            {"type": "text", "text": SCHEMA_HINT + "\n\n逐行提取这张价格表："},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]
    data = llm_client.chat_json(messages, max_tokens=2048)
    return _normalize(data, "image")
