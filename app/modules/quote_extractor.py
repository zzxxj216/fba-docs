"""确定性报价提取（正则）—— 货代文字报价 → QuoteLine[] 结构。

设计见 AGENT_FORWARDER.md §4/§5。一份报价可能：
  - 逐仓多行（ONT8 18/kg、LAX9 20/kg…）
  - 一口价一行（全仓 18/kg）
  - 多单位混报（/kg、/方/cbm、/票）
  - 带起送费/最低收费、时效、截关

输出统一为 dict（不依赖 ORM，便于测试与 LLM 兜底共用同一 schema）：
    {
      "source_type": "text",
      "currency": "CNY",
      "lines": [ {fc, price, currency, unit, channel, eta_days, cutoff,
                   min_charge, remark}, ... ],
      "confidence": 0~1,        # 正则置信度，弱时上层走 LLM 兜底
      "notes": [..],            # 提取过程说明（调试/低置信提示）
    }

陷阱（务必规避）：
  - 仓代码常以数字结尾（ONT8/LAX9/SMF3），别把代码尾数当价格。
  - 时效"7天"、起送费"100元起"、截关日期里的数字都不是运费——先识别这些再抓价。
"""

import re

# ---------------------------------------------------------------- 词典/正则

# FBA 目的仓代码：2~4 字母 + 1~2 数字（ONT8 / LAX9 / SMF3 / FTW1 / BFI4 / MDW2…）
FC_RE = re.compile(r"\b([A-Z]{2,4}\d{1,2})\b")

# 单位归一：把各种写法映射到标准单位
_UNIT_MAP = [
    (re.compile(r"/?\s*(?:per\s*)?(?:kg|公斤|千克|KG|Kg)\b"), "/kg"),
    (re.compile(r"/?\s*(?:per\s*)?(?:cbm|方|立方|m³|立方米|CBM)\b"), "/cbm"),
    (re.compile(r"/?\s*(?:per\s*)?(?:票|单|柜|ticket|shipment)\b"), "/票"),
]

# 货币符号/词 → 代码
_CURRENCY = [
    (re.compile(r"(?:¥|￥|人民币|RMB|CNY|元|块)"), "CNY"),
    (re.compile(r"(?:\$|USD|美元|美金)"), "USD"),
]

# 渠道关键词
_CHANNELS = [
    ("空运", re.compile(r"空运|空派|air(?:\s*freight)?|空运专线", re.I)),
    ("海运", re.compile(r"海运|海派|海卡|sea(?:\s*freight)?|ocean", re.I)),
    ("快递", re.compile(r"快递|快船|express|UPS|FedEx|DHL", re.I)),
    ("铁路", re.compile(r"铁路|铁运|rail", re.I)),
    ("卡航", re.compile(r"卡航|truck", re.I)),
]

# 时效："7天" "7-10天" "7~10个工作日" "ETA 12天"
_ETA_RE = re.compile(r"(?:时效|ETA|预计|大概)?\s*(\d{1,2})\s*[-~到至]?\s*(\d{1,2})?\s*个?\s*(?:工作)?天")

# 起送费/最低收费："100元起" "起运费150" "最低收费 200" "min 50kg"
_MIN_CHARGE_RE = re.compile(
    r"(?:起送费|起运费|起价|最低收费|最低消费|最低|起|min(?:imum)?)\s*[:：]?\s*"
    r"(?:¥|￥|\$|RMB|USD)?\s*(\d+(?:\.\d+)?)\s*(?:元|块)?")

# 截关："截关周三" "截单6/20" "周五截关" "cut[ -]?off 6.20"
_CUTOFF_RE = re.compile(
    r"(?:截关|截单|截重|cut[\s-]?off)\s*[:：]?\s*"
    r"([周一二三四五六日0-9/.\-月号日]+)")

# 裸价/带符号价：捕获一个看起来像运费的数字。
# 形如 18 / 18.5 / ¥18 / 18元/kg / $20。注意：先剥离 FC 代码、时效、起送费再抓。
_PRICE_NEAR_UNIT_RE = re.compile(
    r"(?:¥|￥|\$|RMB|USD|元)?\s*(\d+(?:\.\d+)?)\s*(?:元|块|¥|￥)?\s*"
    r"/?\s*(kg|公斤|千克|KG|Kg|cbm|方|立方|m³|CBM|票|单|柜)")


def _norm_unit(text):
    for rx, std in _UNIT_MAP:
        if rx.search(text):
            return std
    return ""


def _detect_currency(text, default="CNY"):
    for rx, code in _CURRENCY:
        if rx.search(text):
            return code
    return default


def _detect_channel(text):
    for name, rx in _CHANNELS:
        if rx.search(text):
            return name
    return ""


def _detect_eta(text):
    m = _ETA_RE.search(text)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else None
    # 用区间上限作为 eta_days（保守），无区间用单值
    return hi or lo


def _detect_cutoff(text):
    m = _CUTOFF_RE.search(text)
    return m.group(1).strip() if m else ""


def _detect_min_charge(text):
    m = _MIN_CHARGE_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _mask_noise(seg):
    """把 FC 代码、时效、起送费、截关里的数字抹掉，避免被当成运费。

    返回 (masked_text, fc_list)。
    """
    fcs = FC_RE.findall(seg)
    masked = FC_RE.sub(" __FC__ ", seg)
    masked = _ETA_RE.sub(" __ETA__ ", masked)
    masked = _MIN_CHARGE_RE.sub(" __MIN__ ", masked)
    masked = _CUTOFF_RE.sub(" __CUT__ ", masked)
    return masked, fcs


def _extract_price(seg):
    """从一段（已知属于某 FC 或全局）里抓运费价 + 单位。

    优先抓"带单位的价"（18/kg）；没有单位时抓裸数字并默认 /kg。
    返回 (price, unit) 或 (None, "")。
    """
    masked, _ = _mask_noise(seg)

    # 1) 带单位的价
    m = _PRICE_NEAR_UNIT_RE.search(masked)
    if m:
        unit = _norm_unit(m.group(2)) or "/kg"
        return float(m.group(1)), unit

    # 2) 裸价：剩余文本里的第一个数字（噪声已 mask），默认 /kg
    nums = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", masked)
    if nums:
        return float(nums[0]), "/kg"
    return None, ""


def _split_lines(text):
    """把整段报价拆成"逐行/逐句"，便于逐 FC 解析。"""
    parts = re.split(r"[\n;；]+", text)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


def extract(text, lanes=None):
    """主入口：文字报价 → 结构化 dict。

    lanes: 可选，Inquiry 的目的仓清单 [{fc,...}]，用于辅助识别 FC（即便货代未明写代码）。
    """
    text = (text or "").strip()
    notes = []
    if not text:
        return {"source_type": "text", "currency": "CNY", "lines": [],
                "confidence": 0.0, "notes": ["empty"]}

    currency = _detect_currency(text)
    lane_fcs = [str(l.get("fc")).upper() for l in (lanes or []) if l.get("fc")]

    segments = _split_lines(text)
    lines = []
    seen_fc = set()

    for seg in segments:
        seg_fcs = [f.upper() for f in FC_RE.findall(seg)]
        # 只保留 lanes 里真实存在的 FC（若给了 lanes），避免把无关代码当仓
        if lane_fcs:
            seg_fcs = [f for f in seg_fcs if f in lane_fcs] or seg_fcs
        price, unit = _extract_price(seg)
        if price is None:
            continue
        channel = _detect_channel(seg) or _detect_channel(text)
        eta = _detect_eta(seg)
        cutoff = _detect_cutoff(seg)
        min_charge = _detect_min_charge(seg)
        cur = _detect_currency(seg, default=currency)

        if seg_fcs:
            for fc in seg_fcs:
                if fc in seen_fc:
                    continue
                seen_fc.add(fc)
                lines.append({
                    "fc": fc, "price": price, "currency": cur, "unit": unit,
                    "channel": channel, "eta_days": eta, "cutoff": cutoff,
                    "min_charge": min_charge, "remark": "",
                })
        else:
            # 没绑定 FC 的价 —— 一口价候选（fc 留空，比价时按全仓应用）
            lines.append({
                "fc": "", "price": price, "currency": cur, "unit": unit,
                "channel": channel, "eta_days": eta, "cutoff": cutoff,
                "min_charge": min_charge, "remark": "一口价候选" if not seg_fcs else "",
            })

    # 一口价归并：若全部 line 都无 FC 且只有 1 个价，标 fc=ALL（覆盖全仓）
    no_fc = [l for l in lines if not l["fc"]]
    has_fc = [l for l in lines if l["fc"]]
    if lines and not has_fc and len(no_fc) == 1:
        no_fc[0]["fc"] = "ALL"
        no_fc[0]["remark"] = "一口价（全仓）"
        notes.append("flat_rate")

    confidence = _score(lines, lane_fcs, text)
    if not lines:
        notes.append("no_price_found")
    return {"source_type": "text", "currency": currency, "lines": lines,
            "confidence": confidence, "notes": notes}


def _score(lines, lane_fcs, text):
    """正则置信度：抓到价 + FC 命中 lanes + 单位明确 → 越高。

    弱（<0.5）时上层应走 LLM 兜底。
    """
    if not lines:
        return 0.0
    score = 0.4
    priced = [l for l in lines if l.get("price") is not None]
    if priced:
        score += 0.2
    # FC 覆盖 lanes 的比例
    if lane_fcs:
        got = {l["fc"] for l in lines if l["fc"] and l["fc"] != "ALL"}
        flat = any(l["fc"] == "ALL" for l in lines)
        if flat:
            score += 0.25
        else:
            cover = len(got & set(lane_fcs)) / max(1, len(lane_fcs))
            score += 0.25 * cover
    else:
        score += 0.15
    # 单位明确（非默认裸价猜测）
    if any(re.search(r"/kg|/cbm|/票", text) for _ in [0]) or \
       re.search(r"kg|方|cbm|票|公斤|立方", text, re.I):
        score += 0.15
    return round(min(score, 1.0), 3)


def coverage_check(lanes, lines):
    """报全校验：lanes 的 FC 集合 vs 提取出的 QuoteLine.fc。

    返回 {expected, quoted, missing, extra, flat_rate}。
    flat_rate=True 时（有 ALL/空 fc 一口价）视为覆盖全部，missing 为空。
    """
    expected = {str(l.get("fc")).upper() for l in (lanes or []) if l.get("fc")}
    quoted = {str(l.get("fc")).upper() for l in (lines or [])
              if l.get("fc") and str(l.get("fc")).upper() not in ("", "ALL")}
    flat = any(str(l.get("fc")).upper() in ("ALL", "") and l.get("price") is not None
               for l in (lines or []))
    if flat:
        missing = set()
    else:
        missing = expected - quoted
    extra = quoted - expected
    return {
        "expected": sorted(expected),
        "quoted": sorted(quoted),
        "missing": sorted(missing),
        "extra": sorted(extra),
        "flat_rate": flat,
        "complete": not missing and not (extra and expected),
    }
