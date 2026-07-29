"""mcapi 连接配置助手（历史上是 SP-API 建仓客户端，2026-07-29 建仓改走赛狐后仅剩配置读取）。

保留 _env_val/_base/_key 供 sellfox_client（代理模式）、qiwe_client（代理模式）、
coord_client（占坑）复用。SP-API 调用函数已随建仓线移除（见 git 历史）。
"""
import os

_BASE = None
_KEY = None


def _env_val(name):
    """读配置：环境变量优先，退回手工解析 .env（utf-8-sig 兜底 BOM）。"""
    val = (os.getenv(name) or "").strip()
    if not val:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name) and "=" in line and not line.startswith("#"):
                        val = line.partition("=")[2].strip().strip('"').strip("'")
                        break
    return val


def _base():
    """mcapi base url：环境变量 MCAPI_BASE > .env > 默认 8100。"""
    global _BASE
    if _BASE is None:
        _BASE = (_env_val("MCAPI_BASE") or "http://127.0.0.1:8100").rstrip("/")
    return _BASE


def _key():
    """mcapi X-API-Key（每运营一把）：MCAPI_KEY，空=不带头（mcapi 未启用鉴权时兼容）。"""
    global _KEY
    if _KEY is None:
        _KEY = _env_val("MCAPI_KEY")
    return _KEY
