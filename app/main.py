import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .database import BASE_DIR, Base, engine
from .routers import batches, crud, generate, inbound, purchase, sync, templates

Base.metadata.create_all(bind=engine)

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
app.include_router(crud.router, prefix="/api")

app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
