"""管理员导出本地初始化包：python tools/build_seed_pkg.py [输出路径]

产物 seed_pkg.zip（主体/工厂/品牌/产品档案 + RuleConfig 真实值 + doc_rules + 模板原件）
经内网共享/拷贝分发给运营，运营端 POST /api/seed/import-pkg 导入。**绝不提交进 git。**
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.seed import export_pkg  # noqa: E402

db = SessionLocal()
try:
    print(export_pkg(db, sys.argv[1] if len(sys.argv) > 1 else None))
finally:
    db.close()
