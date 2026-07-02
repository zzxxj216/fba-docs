import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TEMPLATE_STORE = os.path.join(BASE_DIR, "templates_store")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(TEMPLATE_STORE, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

DB_URL = os.getenv(
    "DB_URL",
    "mysql+pymysql://root:123456@127.0.0.1:3306/fba_docs?charset=utf8mb4",
)


def _ensure_database():
    """库不存在时自动创建（连到 server 级再建库）。

    仅 MySQL 需要——SQLite（测试用内存/文件库）会自动建库，跳过避免跑 MySQL DDL 报错。
    """
    if not DB_URL.startswith("mysql"):
        return
    server_url, _, db_part = DB_URL.rpartition("/")
    db_name = db_part.split("?")[0]
    tmp = create_engine(server_url, pool_pre_ping=True)
    with tmp.connect() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
    tmp.dispose()


_ensure_database()

engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def _ensure_columns():
    """给已存在的表补新增列（create_all 不改已有表）。幂等，MySQL 专用。"""
    if not DB_URL.startswith("mysql"):
        return
    specs = [
        ("templates", "company_id", "INT NULL"),
        ("templates", "brand_id", "INT NULL"),
        ("brands", "source_address", "TEXT NULL"),
    ]
    try:
        with engine.connect() as conn:
            for table, col, ddl in specs:
                has = conn.execute(text(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE "
                    "TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND COLUMN_NAME=:c"),
                    {"t": table, "c": col}).scalar()
                if not has:
                    conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}"))
                    conn.commit()
    except Exception:
        pass  # 表还没建（fresh DB 由 create_all 带列建好）/无权限时静默


_ensure_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
