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


def welcome_card(operator_name, shops):
    """欢迎/认人卡片：我是谁 + 你是谁管哪些店 + 我能帮你做什么 + 怎么开始。"""
    shop_str = "、".join(f"**{s}**" for s in shops) if shops else "（暂未配置管辖店铺）"
    caps = (
        "📦　**看本周补货**　— 拉赛狐待采购清单\n"
        "🏗️　**建仓**　— 创建入库计划、拿分仓目的仓\n"
        "🚢　**货代询价 / 比价 / 选货代**\n"
        "📄　**生成 / 发送托书**\n"
        "🔍　**查批次进度**"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("👋 亚马逊发货管家", "blue"),
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"你好 **{operator_name}**！我是你的**亚马逊发货管家**。\n"
                           f"你负责 {shop_str}，我帮你把货从**补货**一路管到**发货**。"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**🛠 我能帮你做这些**"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": caps}},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                _btn("📦 查看本周补货", "restock", btn_type="primary"),
                _btn("🔍 查看进度", "view_progress"),
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "也可直接发「看本周补货」「建仓」「查进度」"}]},
        ],
    }


def restock_card(operator_name, plans):
    """本周补货卡片：赛狐待采购/待审核采购计划（已按管辖店铺过滤）。店铺分块、字段分行。

    plans: list[dict]，每条:
      {plan_group_no, status_label, shop_name, brand, item_count, total_qty, skus_sample}
    """
    # 按店铺分组：店铺当大标题，下面挂该店多个采购计划
    by_shop = {}
    for p in plans:
        key = p.get("shop_name") or p.get("brand") or "—"
        by_shop.setdefault(key, []).append(p)
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{operator_name}**，你管辖 **{len(by_shop)}** 个店铺、共 "
                       f"**{len(plans)}** 个采购计划待处理："}}
    if not plans:
        return {"config": {"wide_screen_mode": True},
                "header": _header("📦 本周补货 · 待采购", "blue"),
                "elements": [head, {"tag": "div", "text": {"tag": "lark_md",
                            "content": "✅ 暂无待采购 / 待审核的补货计划。"}}]}
    status_tag = {"待采购": "<font color='orange'>● 待采购</font>",
                  "待审核": "<font color='grey'>● 待审核</font>"}
    elements = [head]
    for shop, splans in by_shop.items():
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"🏪 **{shop}**　·　{len(splans)} 个采购计划"}})
        for p in splans:
            label = p.get("status_label", "")
            tag = status_tag.get(label, label)
            items = p.get("items") or []
            lines = []
            for it in items[:12]:
                lines.append(f"　· `{it.get('sku')}`　**×{it.get('qty', 0)}**")
            if len(items) > 12:
                lines.append(f"　· …共 {len(items)} 个 SKU")
            block = (
                f"▸ **{p.get('plan_group_no')}**　{tag}　· {p.get('item_count', 0)} SKU · "
                f"{p.get('total_qty', 0)} 件\n" + "\n".join(lines)
            )
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
            if label == "待采购":
                elements.append({"tag": "action", "actions": [
                    _btn("✅ 确认采购", "confirm_purchase",
                         {"plan_group_no": p.get("plan_group_no")}, btn_type="primary"),
                ]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "待采购可点【确认采购】；到货后发「建仓」进入下一步"}]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("📦 本周补货 · 待采购", "blue"),
            "elements": elements}


def progress_card(operator_name, items):
    """批次进度卡片：管辖店铺的批次 + 当前状态（建仓/询价等阶段）。

    items: list[dict]，每条 {name, brand, status, batch_id}
    """
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{operator_name}**，你管辖店铺共 **{len(items)}** 个批次："}}
    if not items:
        return {"config": {"wide_screen_mode": True},
                "header": _header("🔍 批次进度", "turquoise"),
                "elements": [head, {"tag": "div", "text": {"tag": "lark_md",
                            "content": "暂无批次。"}}]}
    status_tag = {
        "数据准备": "<font color='grey'>● 数据准备</font>",
        "已建仓": "<font color='green'>● 已建仓</font>",
        "询价中": "<font color='orange'>● 询价中</font>",
        "已选货代": "<font color='blue'>● 已选货代</font>",
        "已完成": "<font color='green'>● 已完成</font>",
    }
    elements = [head]
    for it in items:
        tag = status_tag.get(it.get("status", ""), it.get("status", ""))
        block = (f"📦 **{it.get('name') or ('批次#' + str(it.get('batch_id')))}**　{tag}\n"
                 f"店铺：{it.get('brand', '')}")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
        if it.get("status") == "已建仓" and it.get("batch_id"):
            elements.append({"tag": "action", "actions": [
                _btn("🏗️ 查看分仓方案", "view_placement",
                     {"batch_id": it.get("batch_id")}),
            ]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("🔍 批次进度", "turquoise"),
            "elements": elements}


def placement_card(batch_name, options, batch_id=None):
    """分仓方案卡片：建仓后亚马逊给的各分仓/合仓方案（FC/箱数/重量/placement费）。

    options: list[dict] {placement_option_id, label, fee_usd, shipments:[{fc,city,state,boxes,weight_kg}]}
    给定 batch_id 时，每个方案带【提交此方案+配自送】按钮（默认演练，不碰亚马逊）。
    """
    n = len(options)
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{batch_name}** 建仓后有 **{n}** 个分仓方案"
                       "（留着和货代头程成本一起比，再选方案 + 货代）："}}
    if not options:
        return {"config": {"wide_screen_mode": True},
                "header": _header("🏗️ 分仓方案", "purple"),
                "elements": [head, {"tag": "div", "text": {"tag": "lark_md",
                            "content": "暂无分仓方案（可能还没建仓）。"}}]}
    elements = [head]
    for i, o in enumerate(options, 1):
        shs = o.get("shipments") or []
        fee = o.get("fee_usd") or 0
        lines = [f"**方案{i}**　{o.get('label', '')}　·　placement费 ${fee}"]
        for s in shs[:12]:
            loc = "/".join(x for x in (s.get("city"), s.get("state")) if x)
            loc = f"（{loc}）" if loc else ""
            lines.append(f"　🏬 {s.get('fc')}{loc}　{s.get('boxes', 0)}箱 · {s.get('weight_kg', 0)}kg")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        if batch_id and o.get("placement_option_id"):
            elements.append({"tag": "action", "actions": [
                _btn(f"📤 提交方案{i}+配自送", "commit_placement",
                     {"batch_id": batch_id, "placement_option_id": o.get("placement_option_id")}),
            ]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "询价后按「placement费 + 头程」总成本比，再选方案 + 货代；提交默认演练不碰亚马逊"}]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("🏗️ 分仓方案", "purple"),
            "elements": elements}


def summary_card(operator_name, summary):
    """本周整包询价汇总卡片：所有已建仓批次的全部 FC（方案B：每批所有方案的 FC）。

    summary: {batch_count, fc_count, batches:[{batch_id,name,store,option_count,fcs:[{fc,boxes,weight_kg}]}]}
    """
    batches = summary.get("batches") or []
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{operator_name}** 本周已建仓 **{summary.get('batch_count', 0)}** 个批次、"
                       f"共 **{summary.get('fc_count', 0)}** 个目的仓。整包询价汇总如下："}}
    if not batches:
        return {"config": {"wide_screen_mode": True},
                "header": _header("📋 本周整包询价汇总", "carmine"),
                "elements": [head, {"tag": "div", "text": {"tag": "lark_md",
                            "content": "本周还没有已建仓批次，先去【确认采购】/建仓。"}}]}
    elements = [head]
    for b in batches:
        lines = [f"📦 **{b.get('name')}**　{b.get('store', '')}　（{b.get('option_count', 0)} 个方案）"]
        for f in (b.get("fcs") or [])[:15]:
            lines.append(f"　🏬 {f.get('fc')}　{f.get('boxes', 0)}箱 · {f.get('weight_kg', 0)}kg")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [
        _btn("🚢 整包询价（发货代）", "weekly_inquiry", {}, btn_type="primary"),
    ]})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "汇总齐了点【整包询价】，把全部 FC 一次性发给货代报头程"}]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("📋 本周整包询价汇总", "carmine"),
            "elements": elements}


def weekly_comparison_card(comparison):
    """整包比价卡片：各货代承接整包的总价(每批已自动选最优分仓方案)+ 推荐 + 选定按钮。

    comparison: weekly_comparison 的返回 [{forwarder,currency,total,coverage_ok,recommended,per_batch:[...]}]
    """
    if not comparison:
        return text_card("整包比价", "还没有货代报价。等货代回价后再看比价。", template="orange")
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"共 **{len(comparison)}** 家货代报价，按整包总价排序"
                       "（每批已自动选最优分仓方案 = placement费 + 头程）："}}
    elements = [head]
    for r in comparison:
        rec = "　⭐推荐" if r.get("recommended") else ""
        cov = "" if r.get("coverage_ok") else "　<font color='red'>⚠️有缺仓</font>"
        lines = [f"**{r.get('forwarder')}**{rec}　整包 **{r.get('total')} {r.get('currency', 'USD')}**{cov}"]
        for pb in r.get("per_batch") or []:
            if pb.get("error"):
                lines.append(f"　{pb.get('batch')}：<font color='red'>{pb['error']}</font>")
            else:
                lines.append(f"　{pb.get('batch')}：方案[{pb.get('option')}] "
                             f"placement${pb.get('fee')} + 头程${pb.get('head_haul')} = **${pb.get('total')}**")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        if r.get("coverage_ok"):
            elements.append({"tag": "action", "actions": [
                _btn("✅ 选定这家", "choose_weekly", {"forwarder": r.get("forwarder")},
                     btn_type="primary" if r.get("recommended") else "default"),
            ]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "选定 = 锁定货代 + 各批用其最优分仓方案；提交分仓到亚马逊仍需人工确认"}]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("📊 整包比价 · 请人工选定", "purple"),
            "elements": elements}


def labels_card(operator_name, batches):
    """标签下载卡片：列已建仓批次，每批可下载箱唛/FNSKU（自动剪切成单张给货代）。

    batches: [{batch_id, name}]
    """
    head = {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{operator_name}**，已建仓批次的标签下载（多合一页自动**剪切成单张**给货代）："}}
    if not batches:
        return {"config": {"wide_screen_mode": True},
                "header": _header("🏷️ 标签下载", "indigo"),
                "elements": [head, {"tag": "div", "text": {"tag": "lark_md",
                            "content": "暂无已建仓批次。"}}]}
    elements = [head]
    for b in batches:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"📦 **{b.get('name')}**"}})
        elements.append({"tag": "action", "actions": [
            _btn("📦 箱唛", "fetch_labels", {"batch_id": b.get("batch_id"), "kind": "box"}),
            _btn("🏷️ FNSKU 商品标签", "fetch_labels", {"batch_id": b.get("batch_id"), "kind": "fnsku"}),
        ]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "默认演练；真实下载需开 INBOUND_LIVE_SUBMIT=1。下载后已剪切成单张 PDF"}]})
    return {"config": {"wide_screen_mode": True},
            "header": _header("🏷️ 标签下载（剪切单张）", "indigo"),
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
