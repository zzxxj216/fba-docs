"""qiweapi（第三方企微云端实例）客户端 —— 货代沟通渠道（仿 sellfox_client 风格）。

平台统一入口（POST）：
    {BASE}  body = {"method": "/msg/sendText", "params": {...}}
    Header  X-QIWEI-TOKEN: <控制台应用凭据>

凭据/配置进 .env（勿提交）：
    QIWE_TOKEN   必填，控制台应用 token（缺它=渠道未接通，优雅降级不崩）
    QIWE_GUID    默认实例/设备 id（发消息要带 guid，可在 send_* 显式覆盖）
    QIWE_BASE    可覆盖默认入口 URL

已核对方法（doc.qiweapi.com）：
    /msg/sendText   params: guid, toId(外部联系人或群id), content, isNoNeedRead
命名约定：消息 /msg/*，群 /room/*（用 roomId），朋友圈 /sns/*。
其它接口用通用 call(method, params)，确认 method 串后再加薄封装（避免硬编码错名）。

收消息不在这里：货代回复经平台 Webhook 回调 → 本系统 /api/qiwe/callback 落库，
本模块只管"主动外呼"（发消息 / 拉联系人群组等）。
"""

import os
import threading
import time

import httpx

BASE_DEFAULT = "http://manager.qiweapi.com/qiwe/api/qw/doApi"
MIN_INTERVAL = 0.5                   # 秒，外呼最小间隔（云端实例别打太快）

_last_call = {"ts": 0.0}
_lock = threading.Lock()


def _env(key, default=""):
    """读环境变量；.env 带 BOM 时兜底直读（database.py 已 load_dotenv，这里再保险）。"""
    v = os.getenv(key)
    if v:
        return v
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, val = line.partition("=")
                    if k.strip().lstrip("﻿") == key:
                        return val.strip().strip('"').strip("'")
    return default


def _token():
    return _env("QIWE_TOKEN")


def _base():
    return _env("QIWE_BASE") or BASE_DEFAULT


def default_guid():
    return _env("QIWE_GUID")


def configured():
    """token 是否就绪——上层据此决定是否提示"渠道未接通"，避免直接报错。"""
    return bool(_token())


def _throttle():
    with _lock:
        wait = _last_call["ts"] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call["ts"] = time.time()


def call(method, params=None, timeout=30):
    """统一 POST 调 qiweapi，返回响应里的 data/result。

    未配 token → RuntimeError（上层捕获给"未接通"提示）。
    成功判定对 qiweapi 的返回信封做了容错（code/errcode/success 多种约定都认），
    见到真实返回后可收紧。
    """
    token = _token()
    if not token:
        raise RuntimeError("企微渠道未配置（缺 QIWE_TOKEN），无法收发消息")
    _throttle()
    r = httpx.post(_base(), headers={"X-QIWEI-TOKEN": token},
                   json={"method": method, "params": params or {}}, timeout=timeout)
    try:
        res = r.json()
    except ValueError:
        raise RuntimeError(f"企微接口 {method} 返回非 JSON：HTTP {r.status_code} {r.text[:160]}")
    if not isinstance(res, dict):
        return res
    code = res.get("code", res.get("errcode", 0))
    ok = (str(code) in ("0", "200", "None")) and (res.get("success", True) is not False)
    if not ok:
        msg = res.get("msg") or res.get("message") or res.get("errmsg") or ""
        raise RuntimeError(f"企微接口 {method} 失败 code={code}: {msg}")
    data = res.get("data")
    if data is None:
        data = res.get("result", res)
    return data


# ---------------------------------------------------------------- 已核对：发消息

def send_text(to_id, content, guid=None, no_read=False):
    """发文本到外部联系人或群（toId 即对方 userId 或 roomId）。

    guid 缺省取 QIWE_GUID。返回含 msgServerId（撤回用）/timestamp 等。
    """
    guid = guid or default_guid()
    if not guid:
        raise RuntimeError("缺少实例 guid（配 QIWE_GUID 或显式传入）")
    return call("/msg/sendText", {"guid": guid, "toId": to_id,
                                  "content": content, "isNoNeedRead": bool(no_read)})


# ---------------------------------------------------------------- 已核对：查群

def list_rooms(guid=None):
    """群列表（roomId/roomName/群主/成员数）。配货代群绑定时取 roomId 用。"""
    guid = guid or default_guid()
    if not guid:
        raise RuntimeError("缺少实例 guid（配 QIWE_GUID 或显式传入）")
    r = call("/room/getRoomList", {"guid": guid}) or {}
    return r.get("roomList", [])
