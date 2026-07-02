"""店铺档案体检：每个店铺的全部配置点一屏核对，新店接入必须体检通过再跑流程。

档案信息分两类（防店铺串联的关键约定）：
- **店铺独有(严禁共用/回退)**：SP-API 凭据(按区域)、发货地址(source_address)、店铺主体
  (公司名/地址/电话/USCC)、报关/合同系数(按主体)、赛狐供应商。发货地址曾因回退默认值
  导致 Byane 用了 HUHOLE 的发货地——所以缺失一律报错，绝不用别家的。
- **可共用**：货代及企微群(按主体分群)、文件模板(数据驱动)、工厂(同工厂供货时)、
  规则默认值、mcapi/赛狐服务本身。
"""

import json

from ..models import Brand, Company, Factory, Forwarder, RuleConfig


def _rule(db, key, company_id, default=""):
    row = (db.query(RuleConfig)
           .filter(RuleConfig.key == key, RuleConfig.scope == f"company:{company_id}").first()
           if company_id else None)
    if row:
        return f"{row.value}(主体)"
    g = db.query(RuleConfig).filter(RuleConfig.key == key, RuleConfig.scope == "global").first()
    return f"{g.value}(全局)" if g else default


def check_store(db, brand, *, probe_amazon=True):
    """单店体检。返回 {brand, checks: [(项, ok, 说明)]}。probe_amazon=True 会真调一次列表接口。"""
    checks = []

    # 1) 亚马逊账户(独有)
    store = (brand.amazon_store or "").strip()
    if not store:
        checks.append(("亚马逊账户", False, "amazon_store 未配置"))
    elif probe_amazon:
        try:
            from .. import amazon_fba_client as fba
            fba.call("GET", "/inbound-plans", params={"page_size": 1}, store=store)
            checks.append(("亚马逊账户", True, f"{store} 调通"))
        except Exception as e:
            checks.append(("亚马逊账户", False, f"{store} 调不通: {str(e)[:60]}"))
    else:
        checks.append(("亚马逊账户", True, store))

    # 2) 发货地址(独有，严禁回退)
    addr = None
    try:
        addr = json.loads(brand.source_address) if brand.source_address else None
    except (ValueError, TypeError):
        addr = None
    if addr and (addr.get("address_line1") or "").strip():
        checks.append(("发货地址", True,
                       f"{addr.get('name')} | {addr.get('city')} {addr.get('postal_code')}"))
    else:
        checks.append(("发货地址", False, "缺失——建仓会直接报错(不回退别家地址)"))

    # 3) 店铺主体(独有)
    c = db.get(Company, brand.company_id) if brand.company_id else None
    if c:
        miss = [k for k in ("name_cn", "name_en", "address_cn", "address_en", "phone", "uscc")
                if not (getattr(c, k, None) or "").strip()]
        checks.append(("店铺主体", not miss,
                       f"{c.name_cn}" + (f"，缺{','.join(miss)}" if miss else "")))
    else:
        checks.append(("店铺主体", False, "未绑定 company"))

    # 4) 工厂(可共用，但要绑对)
    f = db.get(Factory, brand.factory_id) if brand.factory_id else None
    checks.append(("工厂", bool(f), f.name if f else "未绑定"))

    # 5) 货代群(可共用，按主体分群)
    fw = (db.query(Forwarder)
          .filter(Forwarder.bind_brand_id == brand.id, Forwarder.active.is_(True)).first())
    if fw and (fw.qiwe_room_id or "").strip():
        checks.append(("货代群", True, f"{fw.name} room={fw.qiwe_room_id}"))
    else:
        checks.append(("货代群", False, "未绑定/缺 room_id——建仓后询价发不出"))

    # 6) 模板集(可共用)
    try:
        tids = json.loads(brand.default_template_ids or "[]")
    except (ValueError, TypeError):
        tids = []
    checks.append(("模板集", bool(tids), str(tids) if tids else "未配置 default_template_ids"))

    # 7) 系数(按主体独立)
    checks.append(("报关加价", True, _rule(db, "customs_markup_factor", brand.company_id)))
    checks.append(("合同系数", True, _rule(db, "contract_price_factor", brand.company_id)))

    # 8) 运营覆盖
    try:
        from ..feishu_models import Operator
        covered = any(o.is_admin or brand.id in json.loads(o.scope_brand_ids or "[]")
                      for o in db.query(Operator).all())
        checks.append(("运营覆盖", covered, "有人管" if covered else "没有运营的 scope 包含该品牌"))
    except Exception:
        checks.append(("运营覆盖", False, "查不到 Operator"))

    # 9) 赛狐供应商(独有，生成采购单用)
    checks.append(("赛狐供应商", bool((brand.sellfox_supplier_id or "").strip()),
                   brand.sellfox_supplier_id or "未配置"))

    return {"brand": brand.name, "ok": all(ok for _, ok, _ in checks), "checks": checks}


def check_all(db, *, probe_amazon=True, only_active=True):
    """全部(在营)店铺体检。"""
    q = db.query(Brand)
    brands = [b for b in q.all() if (b.amazon_store or "").strip() or not only_active]
    return [check_store(db, b, probe_amazon=probe_amazon) for b in brands]


def format_report(results):
    lines = []
    for r in results:
        flag = "✅" if r["ok"] else "❌"
        lines.append(f"{flag} {r['brand']}")
        for name, ok, note in r["checks"]:
            lines.append(f"   {'✓' if ok else '✗'} {name}: {note}")
    return "\n".join(lines)
