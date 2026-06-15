"""赛狐 OpenAPI 客户端（同步版，httpx）。

认证/签名结构与已验证探测脚本一致：
- token：GET /api/oauth/v2/token.json（client_credentials），模块级缓存 24h，提前 5 分钟过期
- 签名：HMAC-SHA256(client_secret, "&".join(sorted("k=v")))，参与签名的有
  access_token / client_id / method / nonce / timestamp / url，
  请求时 query 去掉 method/url 并附 sign
- 限流：官方 1 次/秒，这里强制每次调用间隔 >= 1.1 秒
- code 40001（token 过期）→ 强刷 token 重试一次
- code 40019（触发限流）→ sleep 2s 重试一次
- 其它非 0 code → RuntimeError（带 msg）
"""

import hashlib
import hmac
import os
import random
import threading
import time

import httpx

BASE = "https://openapi.sellfox.com"
TOKEN_TTL = 24 * 3600 - 300          # 24h 提前 5 分钟过期
MIN_INTERVAL = 1.1                   # 秒

_token = {"value": "", "expire_at": 0.0}
_last_call = {"ts": 0.0}
_lock = threading.Lock()


def _credentials():
    """凭据从环境变量读（database.py 已 load_dotenv）；.env 带 BOM 时兜底直读。"""
    cid = os.getenv("SELLFOX_CLIENT_ID") or ""
    sec = os.getenv("SELLFOX_CLIENT_SECRET") or ""
    if cid and sec:
        return cid, sec
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip().lstrip("﻿")
                    v = v.strip().strip('"').strip("'")
                    if k == "SELLFOX_CLIENT_ID" and not cid:
                        cid = v
                    elif k == "SELLFOX_CLIENT_SECRET" and not sec:
                        sec = v
    if not cid or not sec:
        raise RuntimeError("缺少 SELLFOX_CLIENT_ID / SELLFOX_CLIENT_SECRET（检查 .env）")
    return cid, sec


def _throttle():
    """保证两次外呼间隔 >= MIN_INTERVAL 秒。"""
    wait = _last_call["ts"] + MIN_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call["ts"] = time.time()


def _code(res):
    try:
        return int(res.get("code", -1))
    except (TypeError, ValueError):
        return -1


def get_token(force=False):
    """模块级 token 缓存。"""
    with _lock:
        if not force and _token["value"] and time.time() < _token["expire_at"]:
            return _token["value"]
        cid, sec = _credentials()
        _throttle()
        r = httpx.get(
            f"{BASE}/api/oauth/v2/token.json",
            params={"client_id": cid, "client_secret": sec,
                    "grant_type": "client_credentials"},
            timeout=30,
        )
        res = r.json()
        if _code(res) != 0:
            raise RuntimeError(f"赛狐 token 获取失败: {res.get('msg')}")
        _token["value"] = res["data"]["access_token"]
        _token["expire_at"] = time.time() + TOKEN_TTL
        return _token["value"]


def _sign(params, secret):
    data = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def call(path, body=None, _retried_token=False, _retried_limit=False):
    """POST 调用赛狐接口，返回 res['data']；非 0 code 抛 RuntimeError。"""
    cid, sec = _credentials()
    token = get_token()
    sign_params = {
        "access_token": token,
        "client_id": cid,
        "method": "post",
        "nonce": str(random.randint(10000, 99999999)),
        "timestamp": str(int(time.time() * 1000)),
        "url": path,
    }
    query = {k: v for k, v in sign_params.items() if k not in ("method", "url")}
    query["sign"] = _sign(sign_params, sec)
    _throttle()
    r = httpx.post(f"{BASE}{path}", params=query, json=body or {}, timeout=60)
    res = r.json()
    code = _code(res)
    if code == 0:
        return res.get("data")
    if code == 40001 and not _retried_token:        # token 过期 → 强刷重试一次
        get_token(force=True)
        return call(path, body, _retried_token=True, _retried_limit=_retried_limit)
    if code == 40019 and not _retried_limit:        # 限流 → 等 2s 重试一次
        time.sleep(2)
        return call(path, body, _retried_token=_retried_token, _retried_limit=True)
    raise RuntimeError(f"赛狐接口 {path} 失败 code={res.get('code')}: {res.get('msg')}")
