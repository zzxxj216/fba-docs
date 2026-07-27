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


# ---------------------------------------------------------------- 本地初始化包
# 敏感种子（主体档案/规则真实值/doc_rules/模板原件）离线分发，不进 git（OPENSOURCE_PLAN §5）。


def _pkg_models():
    from .models import Brand, Company, Factory, Product, RuleConfig
    return [("companies", Company), ("factories", Factory), ("brands", Brand),
            ("products", Product), ("rule_configs", RuleConfig)]


def export_pkg(db, out_path=None):
    """管理员导出：档案表 + doc_rules 文档 + 模板原件 → seed_pkg.zip。"""
    import json
    import zipfile

    from .database import BASE_DIR, TEMPLATE_STORE
    out_path = out_path or os.path.join(BASE_DIR, "seed_pkg.zip")
    data = {}
    for name, model in _pkg_models():
        rows = []
        for obj in db.query(model).all():
            row = {}
            for c in model.__table__.columns:
                v = getattr(obj, c.name)
                if isinstance(v, (str, int, float, bool)) or v is None:
                    row[c.name] = v
            rows.append(row)
        data[name] = rows
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=1))
        for folder, arc in ((os.path.join(BASE_DIR, "docs", "doc_rules"), "doc_rules"),
                            (TEMPLATE_STORE, "templates")):
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    p = os.path.join(folder, fn)
                    if os.path.isfile(p):
                        z.write(p, f"{arc}/{fn}")
    return {"path": out_path, "tables": {k: len(v) for k, v in data.items()}}


def import_pkg(db, zip_path):
    """运营端导入：按主键 merge upsert（保留原 id——品牌 FK 与 RuleConfig scope 依赖），
    doc_rules → docs/doc_rules，模板 → templates_store（同名覆盖为基线版）。"""
    import json
    import zipfile

    from .database import BASE_DIR, TEMPLATE_STORE
    res = {"tables": {}, "files": 0}
    with zipfile.ZipFile(zip_path) as z:
        data = json.loads(z.read("data.json").decode("utf-8"))
        for name, model in _pkg_models():
            rows = data.get(name) or []
            for row in rows:
                db.merge(model(**row))
            res["tables"][name] = len(rows)
        db.commit()
        for info in z.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("doc_rules/"):
                dst_dir = os.path.join(BASE_DIR, "docs", "doc_rules")
            elif info.filename.startswith("templates/"):
                dst_dir = TEMPLATE_STORE
            else:
                continue
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(info.filename))
            with z.open(info) as src, open(dst, "wb") as out:
                out.write(src.read())
            res["files"] += 1
    return res
