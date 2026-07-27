import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sqlalchemy import text

from .database import BASE_DIR, Base, engine
from .routers import (
    batches, crud, generate, inbound, inquiry, purchase, qiwe, sync, templates,
)

Base.metadata.create_all(bind=engine)


def _ensure_columns():
    """create_all 只建缺失的表、不补已存在表的新列。这里幂等补列（货代企微/绑定字段）。

    仅 MySQL（用 INFORMATION_SCHEMA）；SQLite（测试）由 create_all 直接建全列，跳过。
    """
    if engine.dialect.name != "mysql":
        return
    adds = {
        "forwarders": [
            ("qiwe_external_userid", "VARCHAR(128) DEFAULT ''"),
            ("qiwe_room_id", "VARCHAR(128) DEFAULT ''"),
            ("qiwe_guid", "VARCHAR(64) DEFAULT ''"),
            ("bind_brand_id", "INT NULL"),
            ("is_default", "TINYINT(1) DEFAULT 0"),
            ("active", "TINYINT(1) DEFAULT 1"),
        ],
        # 询价线（Track-A）增量列：create_all 不补已存在表的新列
        "inquiries": [
            ("ref_code", "VARCHAR(64) DEFAULT ''"),
            ("channel", "VARCHAR(8) DEFAULT ''"),
            ("lanes_snapshot", "TEXT NULL"),
        ],
        "inquiry_quotes": [
            ("source_type", "VARCHAR(8) DEFAULT 'text'"),
            ("raw_image_path", "VARCHAR(512) DEFAULT ''"),
            ("customs_fee_monthly", "FLOAT NULL"),
        ],
        "forwarder_messages": [
            ("attribution_status", "VARCHAR(8) DEFAULT ''"),
            ("reply_to_msg_id", "VARCHAR(64) DEFAULT ''"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in adds.items():
            existing = {r[0] for r in conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table})}
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {ddl}"))


_ensure_columns()

app = FastAPI(title="FBA 发货文件管理系统")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """前端为 CDN 单页、改动频繁，禁用静态资源缓存——避免浏览器用旧 app.js
    配新 index.html 导致方法找不到/白屏。API 不缓存也无妨。"""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# 顺序敏感：crud 含通配 /{resource}，必须最后挂
app.include_router(sync.router, prefix="/api")
app.include_router(batches.router, prefix="/api")
app.include_router(templates.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(purchase.router, prefix="/api")
app.include_router(inbound.router, prefix="/api")
app.include_router(qiwe.router, prefix="/api")
app.include_router(inquiry.router, prefix="/api")
app.include_router(crud.router, prefix="/api")

app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
