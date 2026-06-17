"""卡片构造单测 —— 纯函数，不碰 DB/网络。"""

from app.feishu import cards


def _find_buttons(card):
    btns = []
    for el in card["elements"]:
        if el.get("tag") == "action":
            btns.extend(el["actions"])
    return btns


def test_text_card_shape():
    c = cards.text_card("标题", "正文 **md**")
    assert c["header"]["title"]["content"] == "标题"
    assert c["elements"][0]["text"]["content"] == "正文 **md**"


def test_todo_card_empty():
    c = cards.todo_card("张三", [])
    # 无待办时不应有按钮
    assert _find_buttons(c) == []


def test_todo_card_ready_vs_not():
    todos = [
        {"batch_id": 1, "name": "A", "shop_name": "S1", "sku_count": 3,
         "total_qty": 100, "ready": True, "issue_count": 0, "issues": []},
        {"batch_id": 2, "name": "B", "shop_name": "S2", "sku_count": 2,
         "total_qty": 50, "ready": False, "issue_count": 2,
         "issues": ["x：缺HS", "y：缺箱规"]},
    ]
    c = cards.todo_card("李四", todos)
    btns = _find_buttons(c)
    actions = [b["value"]["action"] for b in btns]
    # ready 批次只有 start_inquiry；非 ready 批次多一个 fill_data
    assert actions.count("start_inquiry") == 2
    assert actions.count("fill_data") == 1
    # 按钮带正确 batch_id
    ready_btn = next(b for b in btns if b["value"].get("batch_id") == 1)
    assert ready_btn["value"]["action"] == "start_inquiry"


def test_inquiry_drafted_card_buttons():
    c = cards.inquiry_drafted_card("A", 7, "正文", ["货代甲", "货代乙"])
    btns = _find_buttons(c)
    actions = {b["value"]["action"] for b in btns}
    assert actions == {"send_inquiry", "cancel_inquiry"}
    assert all(b["value"]["inquiry_id"] == 7 for b in btns)


def test_comparison_card_marks_recommended_and_choose_buttons():
    comp = {
        "recommended_quote_id": 9001, "reason": "最低价",
        "quotes": [
            {"quote_id": 9001, "forwarder_name": "A", "total_price": 100,
             "currency": "CNY", "eta_days": 10, "channel": "海运", "missing_fc": []},
            {"quote_id": 9002, "forwarder_name": "B", "total_price": 120,
             "currency": "CNY", "eta_days": 8, "channel": "空运",
             "missing_fc": ["ONT8"], "risk": "涨价"},
        ],
    }
    c = cards.comparison_card("批次X", 5, comp)
    btns = _find_buttons(c)
    assert {b["value"]["action"] for b in btns} == {"choose_forwarder"}
    quote_ids = {b["value"]["quote_id"] for b in btns}
    assert quote_ids == {9001, 9002}
    # 推荐家按钮为 primary
    rec_btn = next(b for b in btns if b["value"]["quote_id"] == 9001)
    assert rec_btn["type"] == "primary"
