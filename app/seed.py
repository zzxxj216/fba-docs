"""种子数据：RuleConfig 规则默认值 + 主体/品牌初始化导入。

规则种子幂等：已存在的 (key, scope='global') 不覆盖（用户改过的值保留）。
品牌工厂导入依赖 Agent A 的 import_service（F:\\聊天记录\\店铺主体-工厂信息库.xlsx），
未就绪时只做规则种子并在响应里说明。
"""

import os

from .models import RuleConfig
from .rule_engine import DEFAULT_RULES

BRAND_FILE = r"F:\聊天记录\店铺主体-工厂信息库.xlsx"


def seed_rules(db):
    """插入规则默认值，已存在不覆盖。返回 {"created": n, "skipped": n}。"""
    created = 0
    skipped = 0
    for key, (value, label) in DEFAULT_RULES.items():
        exists = (db.query(RuleConfig)
                  .filter(RuleConfig.key == key, RuleConfig.scope == "global")
                  .first())
        if exists is not None:
            skipped += 1
            continue
        db.add(RuleConfig(key=key, scope="global", value=value,
                          label=label, default_value=value))
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


def _import_brands(db):
    """尝试调用 Agent A 的 import_service 导入品牌/工厂/主体。"""
    if not os.path.exists(BRAND_FILE):
        return {"imported": False, "msg": f"未找到初始化文件：{BRAND_FILE}"}
    try:
        from .services import import_service
    except ImportError:
        return {"imported": False,
                "msg": "import_service 尚未就绪，本次只做规则种子（集成阶段会接上）"}
    for fn_name in ("import_brand_library", "import_brands_file",
                    "import_brands", "import_brand_factory"):
        fn = getattr(import_service, fn_name, None)
        if fn is None:
            continue
        try:
            ret = fn(db, BRAND_FILE)
            return {"imported": True, "result": ret}
        except Exception as e:
            return {"imported": False, "msg": f"品牌工厂导入失败：{e}"}
    return {"imported": False,
            "msg": "import_service 中未找到品牌导入函数，本次只做规则种子"}


def run_seed(db):
    """POST /api/seed/init 的实现。"""
    rules = seed_rules(db)
    brands = _import_brands(db)
    return {"rules": rules, "brands": brands}
