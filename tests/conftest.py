"""飞书线测试夹具 —— 用独立 SQLite，**不碰共享 MySQL**。

策略：
- 强制询价线走 stub（FEISHU_INQUIRY_STUB=1），飞书线可独立测。
- 建一个内存 SQLite engine，把 database.Base 全部表（含 models + feishu_models）create_all 到它上面，
  提供 `db` 会话夹具。service/intake 都接受显式 db 参数，不依赖全局 SessionLocal。
- 默认不发飞书网络：service._safe_send_card 在 feishu_client.configured() 时会尝试发，
  测试里 monkeypatch fc.configured() → False 或 send_card 抛错由 _safe_send_card 吞掉。
"""

import os

os.environ["FEISHU_INQUIRY_STUB"] = "1"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 触发 Base 注册（导入即把表挂上 Base）
from app.database import Base
from app import models  # noqa: F401
from app import feishu_models  # noqa: F401


@pytest.fixture()
def db(monkeypatch):
    """内存 SQLite 会话；建全部表。每个测试独立。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    # 默认让卡片"发送"成为 no-op：飞书未必接通，测试不发真实网络
    from app import feishu_client as fc
    monkeypatch.setattr(fc, "configured", lambda: False)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
