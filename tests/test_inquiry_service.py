"""询价服务多场景测试（DB 走 SQLite，LLM 默认不接通或 monkeypatch）。

覆盖：起草+ref_code+lanes、发送(mock qiwe)、提取→QuoteLine、整包比价+推荐、选定、追问缺仓。
"""

import json

import pytest

from app import llm_client
from app.models import ForwarderMessage, Inquiry, InquiryQuote, QuoteLine
from app.services import inquiry_service as svc
from conftest import make_batch, make_forwarder


# ------------------------------------------------------------ start_inquiry

def test_start_inquiry_builds_lanes_and_refcode(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)  # 走确定性模板
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    make_forwarder(db, "货代A", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    assert inq.status == "待发送"
    assert inq.ref_code.startswith("INQ-HU")
    lanes = json.loads(inq.lanes_snapshot)
    assert {l["fc"] for l in lanes} == {"ONT8", "LAX9"}
    assert inq.ref_code in inq.content          # 暗号埋进正文
    assert "ONT8" in inq.content
    ids = json.loads(inq.target_forwarder_ids)
    assert len(ids) == 1


def test_start_inquiry_no_lanes_raises(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    from app.models import Batch, Brand
    brand = Brand(name="X", abbr2="XX"); db.add(brand); db.flush()
    batch = Batch(inbound_plan_id="P0", brand_id=brand.id); db.add(batch); db.commit()
    with pytest.raises(ValueError):
        svc.start_inquiry(db, batch.id)


# ------------------------------------------------------------ send_inquiry

def test_send_inquiry_groupcast(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5)])
    f1 = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    f2 = make_forwarder(db, "B", brand_id=batch.brand_id, room_id="R2")
    inq = svc.start_inquiry(db, batch.id)

    sent = []
    def fake_send(db_, fwd, content, inquiry_id=None, batch_id=None, no_read=False):
        sent.append(fwd.id)
        m = ForwarderMessage(forwarder_id=fwd.id, inquiry_id=inquiry_id,
                             batch_id=batch_id, direction="out", content=content,
                             qiwe_msg_id=f"out{fwd.id}")
        db_.add(m); db_.commit()
        return m
    monkeypatch.setattr(svc.forwarder_service, "send_message", fake_send)

    res = svc.send_inquiry(db, inq.id)
    assert set(sent) == {f1.id, f2.id}
    assert res["status"] == "收集中"
    assert all(r["sent"] for r in res["results"])


# ------------------------------------------------------------ 提取 → QuoteLine

def test_extract_text_creates_quote_lines(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3),
                            ("SMF3", 49, 400, 2.5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    text = "ONT8 18元/kg\nLAX9 20元/kg\nSMF3 22元/kg"
    quote = svc.extract_quote_from_text(db, inq.id, f.id, text)
    assert quote.source_type == "text"
    fcs = {l.fc: l.price for l in quote.lines}
    assert fcs == {"ONT8": 18, "LAX9": 20, "SMF3": 22}
    assert quote.extract_confidence >= 0.8


def test_extract_text_reextract_replaces_lines(db, monkeypatch):
    """同一询价同一货代再次提取 → 覆盖旧行，不累积。"""
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    svc.extract_quote_from_text(db, inq.id, f.id, "ONT8 18/kg")
    svc.extract_quote_from_text(db, inq.id, f.id, "ONT8 16/kg")
    assert db.query(InquiryQuote).count() == 1
    assert db.query(QuoteLine).count() == 1
    assert db.query(QuoteLine).first().price == 16


def test_extract_text_llm_fallback_when_regex_weak(db, monkeypatch):
    """正则弱（无价文本）→ 走 LLM 兜底（mock）。"""
    monkeypatch.setattr(llm_client, "available", lambda: True)
    monkeypatch.setattr(svc.quote_extractor_llm, "extract_text", lambda text, lanes=None: {
        "source_type": "text", "currency": "CNY", "customs_fee_monthly": None,
        "lines": [{"fc": "ONT8", "price": 19, "currency": "CNY", "unit": "/kg",
                   "channel": "海运", "eta_days": 30, "cutoff": "", "min_charge": None,
                   "remark": ""}], "confidence": 0.7, "notes": ["llm"]})
    batch = make_batch(db, [("ONT8", 100, 800, 5)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    quote = svc.extract_quote_from_text(db, inq.id, f.id, "运费我口述给你：ONT仓十九块一公斤")
    assert {l.fc: l.price for l in quote.lines} == {"ONT8": 19}


def test_extract_image_uses_vision(db, monkeypatch):
    """图片报价：mock 视觉提取，落 source_type=image。"""
    monkeypatch.setattr(svc.quote_extractor_llm, "extract_image",
                        lambda path, lanes=None: {
        "source_type": "image", "currency": "CNY", "customs_fee_monthly": 500,
        "lines": [{"fc": "ONT8", "price": 18, "currency": "CNY", "unit": "/kg",
                   "channel": "海运", "eta_days": 30, "cutoff": "周三",
                   "min_charge": 100, "remark": ""},
                  {"fc": "LAX9", "price": 20, "currency": "CNY", "unit": "/kg",
                   "channel": "海运", "eta_days": 32, "cutoff": "周三",
                   "min_charge": 100, "remark": ""}],
        "confidence": 0.85, "notes": ["llm"]})
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    f = make_forwarder(db, "A", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    quote = svc.extract_quote_from_image(db, inq.id, f.id, "fake.png")
    assert quote.source_type == "image"
    assert quote.customs_fee_monthly == 500
    assert {l.fc for l in quote.lines} == {"ONT8", "LAX9"}


# ------------------------------------------------------------ 整包比价 + 推荐

def _seed_two_quotes(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    fa = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    fb = make_forwarder(db, "乙", brand_id=batch.brand_id, room_id="R2")
    inq = svc.start_inquiry(db, batch.id)
    # 甲：全仓报全，单价较高
    svc.extract_quote_from_text(db, inq.id, fa.id, "ONT8 20/kg\nLAX9 22/kg")
    # 乙：报全，单价较低 → 应被推荐
    svc.extract_quote_from_text(db, inq.id, fb.id, "ONT8 18/kg\nLAX9 19/kg")
    return inq, fa, fb


def test_get_comparison_package_total_and_recommend(db, monkeypatch):
    inq, fa, fb = _seed_two_quotes(db, monkeypatch)
    cmp = svc.get_comparison(db, inq.id)
    assert cmp["lane_count"] == 2
    # 甲整包 = 20*800 + 22*500 = 16000+11000 = 27000
    # 乙整包 = 18*800 + 19*500 = 14400+9500 = 23900 → 更低
    by = {r["forwarder_name"]: r for r in cmp["quotes"]}
    assert by["甲"]["package_total"] == 27000
    assert by["乙"]["package_total"] == 23900
    # 推荐乙（报全且最低）
    rec_id = cmp["recommended_quote_id"]
    rec = next(r for r in cmp["quotes"] if r["quote_id"] == rec_id)
    assert rec["forwarder_name"] == "乙"
    assert "乙" in cmp["recommendation"]
    # 排序：报全的总价低者在前
    assert cmp["quotes"][0]["forwarder_name"] == "乙"


def test_comparison_marks_missing_warehouse(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    fa = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    fb = make_forwarder(db, "乙", brand_id=batch.brand_id, room_id="R2")
    inq = svc.start_inquiry(db, batch.id)
    svc.extract_quote_from_text(db, inq.id, fa.id, "ONT8 18/kg")          # 缺 LAX9
    svc.extract_quote_from_text(db, inq.id, fb.id, "ONT8 20/kg\nLAX9 22/kg")  # 报全
    cmp = svc.get_comparison(db, inq.id)
    by = {r["forwarder_name"]: r for r in cmp["quotes"]}
    assert by["甲"]["complete"] is False
    assert "LAX9" in by["甲"]["missing"]
    assert by["乙"]["complete"] is True
    # 报全的乙被推荐，即便总价更高
    rec = next(r for r in cmp["quotes"] if r["quote_id"] == cmp["recommended_quote_id"])
    assert rec["forwarder_name"] == "乙"


def test_comparison_flat_rate_covers_all(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    f = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    svc.extract_quote_from_text(db, inq.id, f.id, "全仓统一 18元/kg")
    cmp = svc.get_comparison(db, inq.id)
    row = cmp["quotes"][0]
    assert row["flat_rate"] is True
    assert row["complete"] is True
    # 一口价应用到两仓：18*800 + 18*500 = 23400
    assert row["package_total"] == 23400


def test_comparison_includes_customs_fee(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    monkeypatch.setattr(svc.quote_extractor_llm, "extract_image",
                        lambda path, lanes=None: {
        "source_type": "image", "currency": "CNY", "customs_fee_monthly": 1000,
        "lines": [{"fc": "ONT8", "price": 18, "unit": "/kg", "currency": "CNY",
                   "channel": "", "eta_days": None, "cutoff": "", "min_charge": None,
                   "remark": ""}], "confidence": 0.9, "notes": []})
    batch = make_batch(db, [("ONT8", 100, 800, 5)])
    f = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    svc.extract_quote_from_image(db, inq.id, f.id, "x.png")
    cmp = svc.get_comparison(db, inq.id)
    row = cmp["quotes"][0]
    # 18*800 + 1000 报关 = 15400
    assert row["freight_total"] == 14400
    assert row["customs_fee"] == 1000
    assert row["package_total"] == 15400


def test_min_charge_floor_applied(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    # 小货量，运费低于起送费 → 取起送费
    batch = make_batch(db, [("ONT8", 5, 10, 0.1)])
    f = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    svc.extract_quote_from_text(db, inq.id, f.id, "ONT8 18/kg 起送费500元")
    cmp = svc.get_comparison(db, inq.id)
    # 18*10=180 < 500 → 取 500
    assert cmp["quotes"][0]["package_total"] == 500


# ------------------------------------------------------------ 选定 + 追问

def test_choose_forwarder(db, monkeypatch):
    inq, fa, fb = _seed_two_quotes(db, monkeypatch)
    cmp = svc.get_comparison(db, inq.id)
    qid = cmp["recommended_quote_id"]
    res = svc.choose_forwarder(db, inq.id, qid)
    assert res["chosen_quote_id"] == qid
    assert res["status"] == "已选货代"
    chosen = [q for q in db.query(InquiryQuote).all() if q.is_chosen]
    assert len(chosen) == 1 and chosen[0].id == qid
    assert db.get(Inquiry, inq.id).chosen_quote_id == qid


def test_followup_missing(db, monkeypatch):
    monkeypatch.setattr(llm_client, "available", lambda: False)
    batch = make_batch(db, [("ONT8", 100, 800, 5), ("LAX9", 60, 500, 3)])
    f = make_forwarder(db, "甲", brand_id=batch.brand_id, room_id="R1")
    inq = svc.start_inquiry(db, batch.id)
    quote = svc.extract_quote_from_text(db, inq.id, f.id, "ONT8 18/kg")
    res = svc.followup_missing(db, inq.id, quote.id)
    assert res["missing"] == ["LAX9"]
    assert "LAX9" in res["draft"]
    assert inq.ref_code in res["draft"]
