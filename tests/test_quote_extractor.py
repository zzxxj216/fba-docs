"""确定性报价提取（正则）多场景测试：裸价全仓 / 多单位混报 / 部分报价 / 报全校验。

不依赖 LLM、不连库——纯函数测试。
"""

from app.modules import quote_extractor as qe


LANES = [
    {"fc": "ONT8", "boxes": 100, "weight_kg": 800, "volume_cbm": 5},
    {"fc": "LAX9", "boxes": 60, "weight_kg": 500, "volume_cbm": 3},
    {"fc": "SMF3", "boxes": 49, "weight_kg": 400, "volume_cbm": 2.5},
]


def _by_fc(lines):
    return {l["fc"]: l for l in lines}


def test_bare_price_per_fc_default_kg():
    """裸价（无单位）逐仓 → 默认 /kg，仓代码尾数不被当价格。"""
    text = "ONT8 18\nLAX9 20\nSMF3 22"
    res = qe.extract(text, lanes=LANES)
    by = _by_fc(res["lines"])
    assert set(by) == {"ONT8", "LAX9", "SMF3"}
    assert by["ONT8"]["price"] == 18 and by["ONT8"]["unit"] == "/kg"
    assert by["LAX9"]["price"] == 20
    assert by["SMF3"]["price"] == 22
    # 仓代码 ONT8 的 8、SMF3 的 3 不能被当价格
    assert all(l["price"] in (18, 20, 22) for l in res["lines"])


def test_flat_rate_all_warehouses():
    """一口价（全仓一个价，无逐仓）→ fc=ALL。"""
    text = "全部仓库统一 18元/kg"
    res = qe.extract(text, lanes=LANES)
    assert len(res["lines"]) == 1
    assert res["lines"][0]["fc"] == "ALL"
    assert res["lines"][0]["price"] == 18
    assert res["lines"][0]["unit"] == "/kg"
    cov = qe.coverage_check(LANES, res["lines"])
    assert cov["flat_rate"] is True
    assert cov["missing"] == []
    assert cov["complete"] is True


def test_multi_unit_mixed():
    """多单位混报：/kg、/方(cbm)、/票。"""
    text = (
        "ONT8 海运 18元/kg 时效30天\n"
        "LAX9 海运 1200元/方\n"
        "SMF3 快递 5000/票")
    res = qe.extract(text, lanes=LANES)
    by = _by_fc(res["lines"])
    assert by["ONT8"]["unit"] == "/kg" and by["ONT8"]["price"] == 18
    assert by["LAX9"]["unit"] == "/cbm" and by["LAX9"]["price"] == 1200
    assert by["SMF3"]["unit"] == "/票" and by["SMF3"]["price"] == 5000
    assert by["ONT8"]["channel"] == "海运"
    assert by["SMF3"]["channel"] == "快递"


def test_min_charge_and_eta_not_taken_as_price():
    """起送费/时效里的数字不能被当运费。"""
    text = "ONT8 空运 22元/kg 时效7天 起送费100元"
    res = qe.extract(text, lanes=LANES)
    line = _by_fc(res["lines"])["ONT8"]
    assert line["price"] == 22
    assert line["eta_days"] == 7
    assert line["min_charge"] == 100
    assert line["channel"] == "空运"


def test_eta_range_uses_upper_bound():
    text = "ONT8 18/kg 时效7-12天"
    res = qe.extract(text, lanes=LANES)
    assert _by_fc(res["lines"])["ONT8"]["eta_days"] == 12


def test_partial_quote_missing_warehouse():
    """部分报价（只报了 2 个仓）→ 报全校验标缺 SMF3。"""
    text = "ONT8 18/kg\nLAX9 20/kg"
    res = qe.extract(text, lanes=LANES)
    cov = qe.coverage_check(LANES, res["lines"])
    assert cov["missing"] == ["SMF3"]
    assert cov["complete"] is False
    assert set(cov["quoted"]) == {"ONT8", "LAX9"}


def test_coverage_extra_warehouse():
    """货代报了我们没有的仓 → extra。"""
    lines = [{"fc": "ONT8", "price": 18}, {"fc": "LAX9", "price": 20},
             {"fc": "SMF3", "price": 22}, {"fc": "BFI4", "price": 25}]
    cov = qe.coverage_check(LANES, lines)
    assert cov["extra"] == ["BFI4"]
    assert cov["missing"] == []


def test_currency_detection_usd():
    text = "ONT8 $2.5/kg"
    res = qe.extract(text, lanes=LANES)
    assert res["lines"][0]["currency"] == "USD"
    assert res["lines"][0]["price"] == 2.5


def test_cutoff_detection():
    text = "ONT8 18/kg 截关周三"
    res = qe.extract(text, lanes=LANES)
    assert "周三" in _by_fc(res["lines"])["ONT8"]["cutoff"]


def test_empty_text_low_confidence():
    res = qe.extract("", lanes=LANES)
    assert res["lines"] == []
    assert res["confidence"] == 0.0


def test_garbage_text_low_confidence_triggers_fallback():
    """无价文本 → 低置信（上层据此走 LLM 兜底）。"""
    res = qe.extract("您好，稍等我算一下哈", lanes=LANES)
    assert res["confidence"] < 0.5
    assert res["lines"] == []


def test_full_coverage_high_confidence():
    text = "ONT8 18元/kg\nLAX9 20元/kg\nSMF3 22元/kg"
    res = qe.extract(text, lanes=LANES)
    cov = qe.coverage_check(LANES, res["lines"])
    assert cov["complete"] is True
    assert res["confidence"] >= 0.8
