"""飞书交互卡片构造 —— 纯函数，返回卡片 JSON（dict），不发网络。

卡片 schema 用飞书消息卡片 2.0（config/header/elements）。按钮用 action 元素，
按钮 value 里塞 {action, ...} 给按钮回调（feishu/handlers.py）路由。

安全红线（CONTRACT §4）：任何"最终确认/选定/发文件"都由按钮触发的人工动作，
卡片只呈现选项，不代点。
"""


def _btn(text, action, value_extra=None, btn_type="default"):
    """单个按钮元素。value 里始终带 action，handlers 据此分发。"""
    value = {"action": action}
    if value_extra:
        value.update(value_extra)
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,                      # default/primary/danger
        "value": value,
    }


def _header(title, template="blue"):
    return {"title": {"tag": "plain_text", "content": title}, "template": template}


def text_card(title, body, template="blue"):
    """纯文本提示卡片（无按钮）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, template),
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
    }


def todo_card(operator_name, todos):
    """待办卡片：该运营管辖店铺的待采购/待建仓清单。

    todos: list[dict]，每条:
      {batch_id, name, brand, shop_name, sku_count, total_qty, ready(bool),
       issue_count, issues(list[str])}
    每条带【开始建仓/询价】【帮我补数据】按钮（ready 与否决定主按钮文案/样式）。
    """
    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": f"**{operator_name}**，你管辖店铺共 **{len(todos)}** 个待办："},
    }]
    if not todos:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "暂无待采购/待建仓批次。"}})
        return {"config": {"wide_screen_mode": True},
                "header": _header("建仓就绪体检", "green"), "elements": elements}

    for t in todos:
        ready = t.get("ready")
        status = "✅ 数据就绪" if ready else f"⚠️ {t.get('issue_count', 0)} 项待补"
        lines = [
            f"**{t.get('name') or ('批次#' + str(t.get('batch_id')))}**  ·  {t.get('shop_name', '')}",
            f"SKU {t.get('sku_count', 0)} · 总数量 {t.get('total_qty', 0)} · {status}",
        ]
        issues = t.get("issues") or []
        if issues:
            shown = issues[:3]
            more = f" …等 {len(issues)} 项" if len(issues) > 3 else ""
            lines.append("待补：" + "；".join(shown) + more)
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "\n".join(lines)}})
        primary = _btn(
            "开始建仓询价" if ready else "仍要发起询价",
            "start_inquiry", {"batch_id": t.get("batch_id")},
            btn_type="primary" if ready else "default")
        actions = [primary]
        if not ready:
            actions.append(_btn("帮我补数据", "fill_data",
                                 {"batch_id": t.get("batch_id")}))
        elements.append({"tag": "action", "actions": actions})

    return {"config": {"wide_screen_mode": True},
            "header": _header("建仓就绪体检 · 待办", "green"),
            "elements": elements}


def inquiry_drafted_card(batch_name, inquiry_id, content, forwarder_names):
    """询价已起草卡片：展示待人工确认的正文 + 【确认群发】【取消】按钮。

    群发是"询价类消息可自动发"（CONTRACT §4），但仍走人工点确认，符合红线。
    """
    fwds = "、".join(forwarder_names) if forwarder_names else "（未绑定货代）"
    body = (f"**批次**：{batch_name}\n**拟发给**：{fwds}\n\n"
            f"**询价正文（待确认）**：\n{content}")
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("询价草稿 · 待确认", "orange"),
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "action", "actions": [
                _btn("确认群发", "send_inquiry", {"inquiry_id": inquiry_id},
                     btn_type="primary"),
                _btn("取消", "cancel_inquiry", {"inquiry_id": inquiry_id},
                     btn_type="danger"),
            ]},
        ],
    }


def comparison_card(batch_name, inquiry_id, comparison):
    """整包比价卡片：各货代总价 + 推荐 + 每家【选定】按钮（人工拍板）。

    comparison: {recommended_quote_id, reason, quotes:[{quote_id, forwarder_name,
                 total_price, currency, eta_days, channel, risk, missing_fc}]}
    """
    quotes = comparison.get("quotes") or []
    rec_id = comparison.get("recommended_quote_id")
    elements = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**批次**：{batch_name} · 共 {len(quotes)} 家报价"},
    }]
    reason = comparison.get("reason")
    if reason:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**推荐理由**：{reason}"}})
    for q in quotes:
        is_rec = q.get("quote_id") == rec_id
        tag = "  ⭐推荐" if is_rec else ""
        bits = [f"**{q.get('forwarder_name', '?')}**{tag}"]
        price = q.get("total_price")
        if price is not None:
            bits.append(f"整包 {price} {q.get('currency', 'CNY')}")
        if q.get("channel"):
            bits.append(str(q["channel"]))
        if q.get("eta_days") is not None:
            bits.append(f"{q['eta_days']}天")
        line = " · ".join(bits)
        notes = []
        if q.get("missing_fc"):
            notes.append("缺仓：" + "、".join(q["missing_fc"]))
        if q.get("risk"):
            notes.append("风险：" + str(q["risk"]))
        if notes:
            line += "\n" + "；".join(notes)
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
        elements.append({"tag": "action", "actions": [
            _btn("选定这家", "choose_forwarder",
                 {"inquiry_id": inquiry_id, "quote_id": q.get("quote_id")},
                 btn_type="primary" if is_rec else "default"),
        ]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("整包比价 · 请人工选定", "purple"),
            "elements": elements}
