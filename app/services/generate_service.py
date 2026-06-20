"""文件生成服务：校验前置 → 按模板粒度生成 → 归档 GeneratedDoc。

行为契约（CONTRACTS.md）：
- 生成前强制调用 validate_service.validate_batch；passed=False 直接
  返回 {"blocked": true, "report": ...}，不生成任何文件。
- granularity=shipment：每货件一份，输出 output/{批次名}/{FC}/
- granularity=batch：一份，输出 output/{批次名}/
- granularity=row_per_shipment：单文件，明细区 source="shipments" 每货件一行
- granularity=batch 的模板还可用批次级数据源：source="items_agg"
  （跨货件按 msku 聚合，采购合同=批次总量）、source="plan_items"
  （每 货件×SKU 一行，calc.doc_no 为该货件的，9810 订单导入用）
- requires_forwarder_no 的模板：缺货代单号的货件跳过并记入 errors。
生成失败按模板/货件逐个报错，互不影响。
"""

import json
import os
import re
import time

from .. import excel_engine
from ..database import OUTPUT_DIR, TEMPLATE_STORE
from ..field_registry import build_context, resolve
from ..models import Batch, GeneratedDoc, Template

# filename_rule 短占位符 → 字段路径（带点的占位符直接走 resolve）
_FILENAME_ALIAS = {
    "brand": "batch.brand",
    "batch": "batch.name",
    "fc": "shipment.fc_code",
    "country": "batch.country",
    "date": "batch.contract_date",
    "shipdate": "meta.base_friday_mmdd",
    "MMDD": "meta.base_friday_mmdd",
    "mmdd": "meta.base_friday_mmdd",
    "doc_no": "calc.doc_no",
    "purchase_no": "calc.purchase_no",
}

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n]')


def _safe_name(name):
    name = _ILLEGAL.sub("_", str(name)).strip().strip(".")
    return name or "未命名"


def _render_filename(tmpl, ctx, batch, shipment=None):
    rule = (tmpl.filename_rule or "").strip()
    if not rule:
        mid = (shipment.fc_code if shipment is not None and shipment.fc_code
               else batch.name)
        mmdd = resolve("meta.base_friday_mmdd", ctx)
        name = f"{tmpl.doc_type or '文件'}-{mid}-{mmdd}"
    else:
        def _rep(m):
            token = m.group(1)
            if token == "doc_type":
                return tmpl.doc_type or ""
            path = token if "." in token else _FILENAME_ALIAS.get(token)
            if not path:
                return ""
            try:
                v = resolve(path, ctx)
            except ValueError:
                v = None
            return "" if v is None else str(v)
        name = _TOKEN_RE.sub(_rep, rule)
    name = _safe_name(name)
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return name


def _template_path(tmpl):
    p = tmpl.stored_file or ""
    if not os.path.isabs(p):
        p = os.path.join(TEMPLATE_STORE, p)
    return p


def _run_validate(db, batch_id):
    """调 Agent A 的校验服务；尚未就绪时容错放行（集成阶段接上）。"""
    try:
        from . import validate_service
        return validate_service.validate_batch(db, batch_id)
    except ImportError:
        return {"passed": True, "errors": []}


def _do_one(db, batch, tmpl, mapping, shipment, out_dir, result,
            rows_source=None, persist=True):
    """生成单个文件；失败记 errors 不抛出。返回生成的路径或 None。"""
    label = f"模板「{tmpl.name or tmpl.doc_type}」"
    if shipment is not None:
        label += f" 货件 {shipment.fc_code or shipment.amazon_shipment_id}"
    try:
        ctx = build_context(db, batch, shipment)
        filename = _render_filename(tmpl, ctx, batch, shipment)
        out_path = os.path.join(out_dir, filename)
        excel_engine.fill(_template_path(tmpl), mapping, ctx, out_path,
                          rows_source=rows_source)
        if persist:
            doc = GeneratedDoc(
                batch_id=batch.id,
                shipment_id=shipment.id if shipment is not None else None,
                template_id=tmpl.id,
                doc_type=tmpl.doc_type or "",
                filename=filename,
                path=out_path,
            )
            db.add(doc)
            db.flush()
            result["generated"].append({
                "doc_id": doc.id,
                "filename": filename,
                "shipment_id": shipment.id if shipment is not None else None,
            })
        return out_path
    except Exception as e:  # 逐模板逐货件报错，不互相影响
        result["errors"].append(f"{label} 生成失败：{e}")
        return None


def generate(db, batch_id, template_ids):
    """一键生成。返回 {"generated": [...], "errors": [...]} 或 {"blocked": true}。"""
    batch = db.get(Batch, batch_id)
    if batch is None:
        return {"generated": [], "errors": [f"批次 {batch_id} 不存在"]}

    report = _run_validate(db, batch_id)
    if not report.get("passed", True):
        return {"blocked": True, "report": report}

    result = {"generated": [], "errors": []}
    batch_dir = os.path.join(OUTPUT_DIR, _safe_name(batch.name or f"batch-{batch.id}"))

    for tid in template_ids or []:
        tmpl = db.get(Template, tid)
        if tmpl is None:
            result["errors"].append(f"模板 {tid} 不存在")
            continue
        try:
            mapping = json.loads(tmpl.mapping) if tmpl.mapping else None
        except json.JSONDecodeError as e:
            result["errors"].append(f"模板「{tmpl.name}」映射 JSON 解析失败：{e}")
            continue
        if not mapping:
            result["errors"].append(f"模板「{tmpl.name}」尚未配置映射")
            continue

        gran = tmpl.granularity or "shipment"
        if gran == "shipment":
            for sp in batch.shipments:
                if tmpl.requires_forwarder_no and not (sp.forwarder_order_no or "").strip():
                    # 缺货代单号不再跳过：置空照出，仅留提示（回填单号后可重生）
                    result.setdefault("notes", []).append(
                        f"模板「{tmpl.name}」：货件 {sp.fc_code or sp.amazon_shipment_id} "
                        "无货代单号，已置空生成")
                out_dir = os.path.join(batch_dir, _safe_name(tmpl.doc_type or tmpl.name))
                _do_one(db, batch, tmpl, mapping, sp, out_dir, result)
        elif gran == "batch":
            out_dir = os.path.join(batch_dir, _safe_name(tmpl.doc_type or tmpl.name))
            _do_one(db, batch, tmpl, mapping, None, out_dir, result)
        elif gran == "row_per_shipment":
            # 单文件每货件一行；requires_forwarder_no 时过滤缺单号的货件
            ctx = build_context(db, batch)
            rows = ctx["shipments"]
            if tmpl.requires_forwarder_no:
                # 缺货代单号不再剔除该行：置空照出，仅统计提示
                blanks = sum(1 for row in rows
                             if not ((row.get("shipment") or {}).get("forwarder_order_no") or "").strip())
                if blanks:
                    result.setdefault("notes", []).append(
                        f"模板「{tmpl.name}」：{blanks} 个货件无货代单号，已置空生成")
            if not rows:
                result["errors"].append(f"模板「{tmpl.name}」：没有可生成的货件行")
                continue
            out_dir = os.path.join(batch_dir, _safe_name(tmpl.doc_type or tmpl.name))
            _do_one(db, batch, tmpl, mapping, None, out_dir, result,
                    rows_source={"shipments": rows})
        else:
            result["errors"].append(f"模板「{tmpl.name}」未知生成粒度 '{gran}'")

    db.commit()
    return result


def preview(db, batch_id, template_id, table_rows_limit=5):
    """生成前数据预览：加载模板映射 + build_context + excel_engine.preview，
    **不写文件**，返回按货件分组（batch 粒度单组）的将写入清单。

    返回 {"template": {...}, "granularity": g, "groups": [
        {"label", "shipment_id", "filename", "skipped"?,
         "cells": [...], "tables": [...], "errors": [...]}]}
    """
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise ValueError(f"批次 {batch_id} 不存在")
    tmpl = db.get(Template, template_id)
    if tmpl is None:
        raise ValueError(f"模板 {template_id} 不存在")
    try:
        mapping = json.loads(tmpl.mapping) if tmpl.mapping else None
    except json.JSONDecodeError as e:
        raise ValueError(f"模板「{tmpl.name}」映射 JSON 解析失败：{e}")
    if not mapping:
        raise ValueError(f"模板「{tmpl.name}」尚未配置映射")

    tpath = _template_path(tmpl)
    gran = tmpl.granularity or "shipment"
    groups = []

    def _group(shipment, rows_source=None, label=None):
        ctx = build_context(db, batch, shipment)
        g = excel_engine.preview(tpath, mapping, ctx, rows_source=rows_source,
                                 table_rows_limit=table_rows_limit)
        g["label"] = label or (
            (shipment.fc_code or shipment.amazon_shipment_id or f"货件{shipment.id}")
            if shipment is not None else "批次级")
        g["shipment_id"] = shipment.id if shipment is not None else None
        g["filename"] = _render_filename(tmpl, ctx, batch, shipment)
        return g

    if gran == "shipment":
        for sp in batch.shipments:
            if tmpl.requires_forwarder_no and not (sp.forwarder_order_no or "").strip():
                groups.append({
                    "label": sp.fc_code or sp.amazon_shipment_id or f"货件{sp.id}",
                    "shipment_id": sp.id, "filename": "",
                    "skipped": "缺货代单号，生成时将跳过（回填后可生成）",
                    "cells": [], "tables": [], "errors": []})
                continue
            groups.append(_group(sp))
    elif gran == "batch":
        groups.append(_group(None))
    elif gran == "row_per_shipment":
        ctx0 = build_context(db, batch)
        rows = ctx0["shipments"]
        skipped_fcs = []
        if tmpl.requires_forwarder_no:
            kept = []
            for sp, row in zip(sorted(batch.shipments or [],
                                      key=lambda s: (s.fc_code or "", s.id)), rows):
                if (sp.forwarder_order_no or "").strip():
                    kept.append(row)
                else:
                    skipped_fcs.append(sp.fc_code or sp.amazon_shipment_id)
            rows = kept
        g = _group(None, rows_source={"shipments": rows})
        if skipped_fcs:
            g["errors"].insert(0, "缺货代单号的货件行已跳过：" + "、".join(skipped_fcs))
        groups.append(g)
    else:
        raise ValueError(f"模板「{tmpl.name}」未知生成粒度 '{gran}'")

    return {
        "template": {"id": tmpl.id, "name": tmpl.name,
                     "doc_type": tmpl.doc_type, "granularity": gran},
        "granularity": gran,
        "groups": groups,
    }


def test_generate(db, template_id, batch_id):
    """试生成：单模板，不落 GeneratedDoc，文件放 output/_test/。

    返回 (out_path, filename) 或抛 ValueError。
    """
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise ValueError(f"批次 {batch_id} 不存在")
    tmpl = db.get(Template, template_id)
    if tmpl is None:
        raise ValueError(f"模板 {template_id} 不存在")
    try:
        mapping = json.loads(tmpl.mapping) if tmpl.mapping else None
    except json.JSONDecodeError as e:
        raise ValueError(f"映射 JSON 解析失败：{e}")
    if not mapping:
        raise ValueError("模板尚未配置映射")

    gran = tmpl.granularity or "shipment"
    shipment = None
    rows_source = None
    if gran == "shipment":
        if not batch.shipments:
            raise ValueError("批次没有货件，无法试生成")
        shipment = batch.shipments[0]

    ctx = build_context(db, batch, shipment)
    if gran == "row_per_shipment":
        rows_source = {"shipments": ctx["shipments"]}

    filename = _render_filename(tmpl, ctx, batch, shipment)
    out_dir = os.path.join(OUTPUT_DIR, "_test")
    out_path = os.path.join(out_dir, f"{int(time.time())}_{filename}")
    excel_engine.fill(_template_path(tmpl), mapping, ctx, out_path,
                      rows_source=rows_source)
    return out_path, filename
