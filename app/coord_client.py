"""关键节点占坑客户端（mcapi 协调层，OPENSOURCE_PLAN Phase 1.5）。

多运营各自本地库互不可见——真实写操作（建仓/下采购单/确认分仓/发询价）前先到服务器
占坑（scope_key 唯一），防两人重复操作同一对象（红线背景：重复建出的亚马逊入库计划/
赛狐采购单不能自动取消，只能人工清理）。

启用条件：配了 MCAPI_KEY（新架构信号）。未配 = 单机模式，占坑整体跳过（现状兼容）。
fail-closed：启用后服务器不可达 → 阻止写操作（防脱网双开）；紧急旁路 CHECKPOINT_BYPASS=1。
"""
import httpx

from .amazon_fba_client import _base, _env_val, _key


def enabled():
    return bool(_key()) and _env_val("CHECKPOINT_BYPASS") != "1"


def claim(scope_key, node="", refs=None):
    """占坑。他人已占 → RuntimeError（含先占者信息）；服务器不可达 → RuntimeError
    （fail-closed）。未启用 → 直接放行返回 None。本人重复 claim 幂等。"""
    if not enabled():
        return None
    try:
        r = httpx.post(f"{_base()}/api/v1/checkpoints",
                       json={"scope_key": scope_key, "node": node, "refs": refs or {}},
                       headers={"X-API-Key": _key()}, timeout=15)
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"协调服务(mcapi)不可达，为防多人重复操作已阻止本次写入：{e}"
            "（服务器恢复后重试；确认无他人并行操作时可临时设 CHECKPOINT_BYPASS=1）")
    if r.status_code == 409:
        try:
            msg = r.json().get("message") or ""
        except ValueError:
            msg = r.text[:200]
        raise RuntimeError(f"操作冲突：{msg}——他人已做过或正在做，勿重复执行；"
                           "如确需接管请联系管理员处理占坑记录")
    if r.status_code >= 400:
        raise RuntimeError(f"协调服务占坑失败 HTTP {r.status_code}: {r.text[:200]}")
    try:
        return (r.json() or {}).get("data")
    except ValueError:
        return None


def report(scope_key, refs=None, status="done", node=""):
    """成功后回填标识号（追溯链）。尽力而为：失败只打印，不阻塞业务。"""
    if not enabled():
        return
    try:
        httpx.post(f"{_base()}/api/v1/checkpoints",
                   json={"scope_key": scope_key, "node": node,
                         "refs": refs or {}, "status": status},
                   headers={"X-API-Key": _key()}, timeout=15)
    except Exception as e:
        print(f"[coord] 节点回填失败 {scope_key}: {e}", flush=True)
