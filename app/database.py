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
    """库不存在时自动创建（连到 server 级再建库）。"""
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
