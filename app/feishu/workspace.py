"""运营工作区文档：每运营每周一个文件夹，总览.md 记录每个采购计划/批次的进度。

结构（见规划）：
  output/工作区/{运营}/{周 2026-W25}/
    state.json            结构化事实（总览.md 从它生成，可回放、可追溯）
    总览.md               所有批次的进度表（到哪一步 + 错误数）
    {店铺}/{计划号}/
      概况.md             该批次详情 + 进度时间线
      日志.md             操作 + 错误日志
      files/              生成的文件（托书等）

设计：state.json 是唯一事实源；总览.md / 概况.md 每次从 state 重新生成（只读快照）。
所有写操作幂等：同一 plan_group_no 反复 record 只更新、不重复建。
"""

import json
import os
import re
from datetime import datetime

from ..database import OUTPUT_DIR

ROOT = os.path.join(OUTPUT_DIR, "工作区")

# 阶段顺序（总览展示用）
STAGES = ["待采购", "已确认采购", "采购单已生成", "已建仓", "询价中",
          "已选货代", "已发托书", "完成"]


def _safe(name):
    """文件名/目录名安全化（去掉非法字符）。"""
    return re.sub(r'[\\/:*?"<>|]', "_", str(name or "")).strip() or "_"


def week_tag(dt=None):
    """ISO 周标签，如 2026-W25。"""
    dt = dt or datetime.now()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def week_dir(operator, dt=None):
    d = os.path.join(ROOT, _safe(operator), week_tag(dt))
    os.makedirs(d, exist_ok=True)
    return d


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load(wd):
    p = os.path.join(wd, "state.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {"plans": {}}


def _save(wd, state):
    with open(os.path.join(wd, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _plan_dir(wd, rec):
    d = os.path.join(wd, _safe(rec.get("shop")), _safe(rec.get("plan_group_no")))
    os.makedirs(os.path.join(d, "files"), exist_ok=True)
    return d


def record_plan(operator, plan_group_no, *, shop="", sku_count=None, qty=None,
                stage=None, note=""):
    """登记/更新一个采购计划在本周工作区的进度。返回 {week_dir, plan_dir, rec}。"""
    wd = week_dir(operator)
    state = _load(wd)
    rec = state["plans"].get(plan_group_no) or {
        "plan_group_no": plan_group_no, "shop": shop, "sku_count": sku_count,
        "qty": qty, "stage": "待采购", "history": [], "errors": [], "files": [],
        "created_at": _now()}
    if shop:
        rec["shop"] = shop
    if sku_count is not None:
        rec["sku_count"] = sku_count
    if qty is not None:
        rec["qty"] = qty
    if stage and stage != rec.get("stage"):
        rec["stage"] = stage
        rec["history"].append({"ts": _now(), "stage": stage, "note": note})
    elif stage and note:
        rec["history"].append({"ts": _now(), "stage": stage, "note": note})
    state["plans"][plan_group_no] = rec
    _save(wd, state)
    _write_plan_doc(wd, rec)
    _write_overview(wd, operator, state)
    return {"week_dir": wd, "plan_dir": _plan_dir(wd, rec), "rec": rec}


def log(operator, plan_group_no, message, *, level="info", shop=""):
    """给某批次记一条日志（操作/错误）。error 级会计入总览的错误数。"""
    wd = week_dir(operator)
    state = _load(wd)
    rec = state["plans"].get(plan_group_no)
    if rec is None:
        rec = {"plan_group_no": plan_group_no, "shop": shop, "stage": "",
               "history": [], "errors": [], "files": [], "created_at": _now()}
        state["plans"][plan_group_no] = rec
    entry = {"ts": _now(), "level": level, "msg": str(message)[:500]}
    if level == "error":
        rec["errors"].append(entry)
    state["plans"][plan_group_no] = rec
    _save(wd, state)
    _append_log(wd, rec, entry)
    _write_overview(wd, operator, state)


def add_file(operator, plan_group_no, file_path):
    """登记一个产出文件到该批次（路径记到 state + 概况）。"""
    wd = week_dir(operator)
    state = _load(wd)
    rec = state["plans"].get(plan_group_no)
    if rec is None:
        return
    rec.setdefault("files", []).append({"ts": _now(), "path": file_path})
    _save(wd, state)
    _write_plan_doc(wd, rec)


def write_weekly_summary(operator, summary):
    """把本周整包询价汇总(所有已建仓批次的全部 FC)写进主文件夹：本周分仓汇总.md。"""
    wd = week_dir(operator)
    batches = summary.get("batches") or []
    lines = [f"# {operator} 本周整包询价汇总（{os.path.basename(wd)}）", "",
             f"更新：{_now()}　|　已建仓批次 **{summary.get('batch_count', 0)}** 个 · "
             f"目的仓合计 **{summary.get('fc_count', 0)}** 个", "",
             "> 方案B：每批列出所有分仓方案涉及的全部 FC，货代逐 FC 报头程，"
             "再按 placement费+头程 总成本选方案+货代。", ""]
    for b in batches:
        lines.append(f"## {b.get('name')}　{b.get('store', '')}　（{b.get('option_count', 0)} 个方案）")
        lines.append("| 目的仓FC | 箱数 | 重量kg |")
        lines.append("|---|---|---|")
        for f in (b.get("fcs") or []):
            lines.append(f"| {f.get('fc')} | {f.get('boxes', 0)} | {f.get('weight_kg', 0)} |")
        lines.append("")
    with open(os.path.join(wd, "本周分仓汇总.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return os.path.join(wd, "本周分仓汇总.md")


# ---------------------------------------------------------------- 文档生成

def _write_plan_doc(wd, rec):
    d = _plan_dir(wd, rec)
    lines = [
        f"# 采购计划 {rec.get('plan_group_no')}", "",
        f"- 店铺：{rec.get('shop', '')}",
        f"- SKU 数：{rec.get('sku_count', '')}",
        f"- 总数量：{rec.get('qty', '')}",
        f"- 当前阶段：**{rec.get('stage', '')}**",
        f"- 创建：{rec.get('created_at', '')}",
        "", "## 进度时间线", "",
    ]
    for h in rec.get("history", []):
        lines.append(f"- {h['ts']}　**{h['stage']}**" + (f"　{h['note']}" if h.get("note") else ""))
    files = rec.get("files", [])
    if files:
        lines += ["", "## 产出文件", ""]
        lines += [f"- {f['ts']}　{f['path']}" for f in files]
    errs = rec.get("errors", [])
    if errs:
        lines += ["", "## 错误", ""]
        lines += [f"- {e['ts']}　{e['msg']}" for e in errs]
    with open(os.path.join(d, "概况.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _append_log(wd, rec, entry):
    d = _plan_dir(wd, rec)
    with open(os.path.join(d, "日志.md"), "a", encoding="utf-8") as f:
        f.write(f"- {entry['ts']} `[{entry['level']}]` {entry['msg']}\n")


def _write_overview(wd, operator, state):
    plans = sorted(state["plans"].values(),
                   key=lambda x: (x.get("shop", ""), x.get("plan_group_no", "")))
    lines = [
        f"# {operator} 本周工作总览（{os.path.basename(wd)}）", "",
        f"更新：{_now()}　|　共 **{len(plans)}** 个采购计划", "",
        "| 店铺 | 采购计划 | SKU | 数量 | 当前阶段 | 错误 |",
        "|---|---|---|---|---|---|",
    ]
    for r in plans:
        err = len(r.get("errors", []))
        errcell = f"⚠️ {err}" if err else ""
        lines.append("| {shop} | {pgn} | {sku} | {qty} | {stage} | {err} |".format(
            shop=r.get("shop", ""), pgn=r.get("plan_group_no", ""),
            sku=r.get("sku_count", "") if r.get("sku_count") is not None else "",
            qty=r.get("qty", "") if r.get("qty") is not None else "",
            stage=r.get("stage", ""), err=errcell))
    lines += ["", "> 明细见各 `店铺/计划号/概况.md`；错误见同目录 `日志.md`。"]
    with open(os.path.join(wd, "总览.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
