"""规则引擎 —— 所有业务规则的唯一出口（CLAUDE.md 约定 3：规则不许硬编码）。

规则值优先从 RuleConfig 表读取：scope='company:{id}'（主体级覆盖）> scope='global'。
表里没有时使用内置默认值 DEFAULT_RULES（seed.py 据此插种子数据）。
其他模块需要日期/编号/净重/加价/保价等业务规则时一律调用本模块。
"""

import calendar
import re
from datetime import date, datetime, timedelta

from .models import Company, RuleConfig

# key: (默认值, 中文名)。与 seed.py 共用。
DEFAULT_RULES = {
    "net_weight_deduction": ("0.5", "净重扣减(kg/箱)"),
    "vat_factor": ("1.13", "增值税系数"),
    "markup_factor": ("1.10", "二级合同加价系数"),
    # 采购合同单价系数：合同单价 = 采购成本 × 系数（不四舍五入，保留全精度，
    # 对照 RPA 成品口径：Serenorch=1.0(原价)、舟峰/保峰系=1.695(=1.5×1.13)）。
    # 主体级覆盖：scope='company:{id}'。
    "contract_price_factor": ("1.0", "采购合同单价系数(成本×系数)"),
    # 报关价计算：报关价($) = ROUND(采购价×vat×markup×markup÷汇率,1) + ROUND(箱重÷每箱数÷汇率×系数,1)
    "customs_markup_factor": ("1.10", "报关价加价系数(应用两次)"),
    "customs_exchange_rate": ("7", "报关价汇率(¥→$)"),
    "customs_weight_coeff": ("10", "报关价重量分摊系数"),
    "origin_country": ("中国", "原产国"),
    "dest_country": ("美国", "运抵国"),
    "unit_name": ("盒", "申报计量单位"),
    "currency_customs": ("USD", "报关币制"),
    "package_type": ("纸箱", "包装种类"),
    "supervision_mode": ("跨境电商出口海外仓", "监管方式"),
    "tax_mode": ("照章征税", "征免性质"),
    "transport_mode": ("海运", "运输方式"),
    "delivery_mode": ("卡车派", "派送方式"),
    "dest_type": ("FBA", "目的地类型"),
    "shelf_guarantee": ("保上架", "上架保障"),
    "departure_port": ("宁波", "起运港"),
    "fragile": ("无易碎品", "易碎品"),
    "insurance_currency": ("人民币", "保险币种"),
    "insurance_ratio": ("100%", "投保比例"),
    # 报关资料境外收货人：默认 BYANE CO., LIMITED；Serenorch(主体7玫玑研)按
    # company:7 覆盖为 AMAZON.COM SERVICES, INC.（见 docs/doc_rules/报关资料.md）
    "overseas_consignee": ("BYANE CO., LIMITED", "境外收货人"),
}


# ---------------------------------------------------------------- 基础读取

def get_rule(db, key, company_id=None):
    """读取规则值：company:{id} 覆盖 global，表里没有用内置默认。返回字符串。"""
    if db is not None:
        if company_id:
            row = (db.query(RuleConfig)
                   .filter(RuleConfig.key == key,
                           RuleConfig.scope == f"company:{company_id}")
                   .first())
            if row is not None and str(row.value or "").strip() != "":
                return row.value
        row = (db.query(RuleConfig)
               .filter(RuleConfig.key == key, RuleConfig.scope == "global")
               .first())
        if row is not None and str(row.value or "").strip() != "":
            return row.value
    default = DEFAULT_RULES.get(key)
    return default[0] if default else ""


def get_rule_float(db, key, company_id=None, fallback=0.0):
    try:
        return float(str(get_rule(db, key, company_id)).strip())
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------- 日期规则

def _to_date(d):
    """date/datetime/'YYYY-MM-DD'/'YYYY/MM/DD' → date。"""
    if d is None:
        return date.today()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip().replace("/", "-")
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def next_friday(d=None):
    """发货基准日：周一~四→本周五；周五→当天；周六日→下周五。"""
    d = _to_date(d)
    wd = d.weekday()  # 周一=0 … 周五=4 周六=5 周日=6
    if wd == 4:
        return d
    if wd < 4:
        return d + timedelta(days=4 - wd)
    return d + timedelta(days=4 - wd + 7)


def prev_month_same_day(d):
    """合同日期：上月同日；day 超过上月末日则取上月末日（31→30、3-31→2-28/29）。"""
    d = _to_date(d)
    if d.month > 1:
        y, m = d.year, d.month - 1
    else:
        y, m = d.year - 1, 12
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


# ---------------------------------------------------------------- 数值规则

def net_weight(gross, boxes, db=None, company_id=None):
    """净重 = 毛重 − 箱数×扣减（默认 0.5kg/箱）。"""
    deduction = get_rule_float(db, "net_weight_deduction", company_id, 0.5)
    return round((gross or 0) - (boxes or 0) * deduction, 2)


def price_vat(cost, db=None, company_id=None):
    """一级合同含税单价 = 采购成本 × 增值税系数（默认 1.13）。"""
    if cost is None:
        return None
    return round(cost * get_rule_float(db, "vat_factor", company_id, 1.13), 2)


def price_vat_markup(cost, db=None, company_id=None):
    """二级合同含税加价单价 = 采购成本 × 1.13 × 1.10。"""
    if cost is None:
        return None
    vat = get_rule_float(db, "vat_factor", company_id, 1.13)
    markup = get_rule_float(db, "markup_factor", company_id, 1.10)
    return round(cost * vat * markup, 2)


def price_contract(cost, db=None, company_id=None):
    """采购合同单价 = 采购成本 × contract_price_factor（**不四舍五入**——
    HUHOLE/BFPeaky 成品合同单价为全精度，如 21.61×1.695=36.62895）。"""
    if cost is None:
        return None
    return cost * get_rule_float(db, "contract_price_factor", company_id, 1.0)


def customs_price(cost, box_weight_kg, qty_per_box, db=None, company_id=None):
    """报关价($) = ROUND(采购价×增值税×加价×加价÷汇率, 1) + ROUND(箱重÷每箱数÷汇率×系数, 1)。

    第一项=货值口径(采购价加价后换美元)，第二项=重量分摊。系数全部走 RuleConfig。
    采购价缺失返回 None；重量/每箱数缺失时第二项按 0。"""
    if cost is None:
        return None
    vat = get_rule_float(db, "vat_factor", company_id, 1.13)
    # 两道加价：customs_markup_factor 可调（用户改的那个 1.1→1.5），markup_factor 固定二级加价(1.1)
    markup1 = get_rule_float(db, "customs_markup_factor", company_id, 1.10)
    markup2 = get_rule_float(db, "markup_factor", company_id, 1.10)
    exch = get_rule_float(db, "customs_exchange_rate", company_id, 7.0) or 7.0
    coeff = get_rule_float(db, "customs_weight_coeff", company_id, 10.0)
    part1 = round(cost * vat * markup1 * markup2 / exch, 1)
    part2 = 0.0
    if box_weight_kg and qty_per_box:
        part2 = round(box_weight_kg / qty_per_box / exch * coeff, 1)
    return round(part1 + part2, 1)


def insurance_price(unit_price, company):
    """保价单价 = 申报单价 × 主体保价倍率（Company.insurance_factor）。"""
    if unit_price is None:
        return None
    factor = (company.insurance_factor if company is not None
              and company.insurance_factor is not None else 1.0)
    return round(unit_price * factor, 2)


# ---------------------------------------------------------------- 编号规则

def _mmdd(d):
    try:
        return _to_date(d).strftime("%m%d")
    except (ValueError, TypeError):
        return str(d or "")


def _full_date(d):
    try:
        return _to_date(d).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(d or "")


def doc_no(brand, company, fc, contract_date, kind, db=None,
           country=None, base_date=None):
    """按 Brand.doc_no_rule_shipment / doc_no_rule_purchase 模板渲染编号。

    占位符：{sa} {brand2} {fc} {date}=合同日期 {country} {shipdate}=基准日。
    {sa}：company.export_via_trade 时取外贸主体(type=trade)的 abbr+"-"，否则空。
    {date} 渲染为全日期 YYYY-MM-DD（历史成品 9810 订单编号 SE-ABE8-2026-05-06）；
    需要月日形态时用 {date_mmdd}（MMDD，如 0519）。{shipdate} 仍为基准日 MMDD。
    """
    if kind == "purchase":
        template = (getattr(brand, "doc_no_rule_purchase", None)
                    or "{sa}{brand2}-{country}-{date}")
    else:
        template = (getattr(brand, "doc_no_rule_shipment", None)
                    or "{sa}{brand2}-{fc}-{date}")

    sa = ""
    if company is not None and getattr(company, "export_via_trade", False):
        trade = None
        if db is not None:
            trade = (db.query(Company)
                     .filter(Company.type == "trade", Company.active == True)  # noqa: E712
                     .first())
        abbr = (trade.abbr if trade is not None and trade.abbr else "SA")
        sa = abbr + "-"

    brand2 = ""
    if brand is not None:
        brand2 = brand.abbr2 or (brand.name or "")[:2].upper()

    values = {
        "sa": sa,
        "brand2": brand2,
        "fc": fc or "",
        "date": _full_date(contract_date),
        "date_mmdd": _mmdd(contract_date),
        "country": country or "US",
        "shipdate": _mmdd(base_date) if base_date else "",
    }

    def _rep(m):
        return str(values.get(m.group(1), ""))

    return re.sub(r"\{([a-z0-9_]+)\}", _rep, template)
