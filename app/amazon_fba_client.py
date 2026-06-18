"""mcapi（multi-channel-api）建仓服务的 HTTP 客户端。

D:\amazon 不直接集成 async SP-API SDK，而是 HTTP 调用独立运行的 mcapi 服务
（F:/练习模块/multi-channel-api，已把亚马逊 FBA 全流程封装为 /api/v1/amazon/fba/*）。
base url 在 .env `MCAPI_BASE`（默认 http://127.0.0.1:8100）。

mcapi 的 generate/confirm 操作是异步的，返回 operationId，需轮询
GET /operations/{id} 到 SUCCESS。wait_operation 封装轮询。
"""

import os
import time

import httpx

API_PREFIX = "/api/v1/amazon/fba"
_BASE = None


def _base():
    """mcapi base url：环境变量 MCAPI_BASE > .env（带 BOM 兜底）> 默认 8100。"""
    global _BASE
    if _BASE is not None:
        return _BASE
    val = (os.getenv("MCAPI_BASE") or "").strip()
    if not val:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MCAPI_BASE") and "=" in line and not line.startswith("#"):
                        val = line.partition("=")[2].strip().strip('"').strip("'")
                        break
    _BASE = (val or "http://127.0.0.1:8100").rstrip("/")
    return _BASE


def call(method, path, params=None, json=None, store=None, timeout=60):
    """调 mcapi /amazon/fba 接口；store 注入为 ?store=。返回 ApiResponse.data。

    连接失败 / 4xx-5xx / 业务错误均抛 RuntimeError（路由统一转 502）。
    """
    url = f"{_base()}{API_PREFIX}{path}"
    q = dict(params or {})
    if store:
        q["store"] = store
    try:
        r = httpx.request(method, url, params=q, json=json, timeout=timeout)
    except httpx.HTTPError as e:
        raise RuntimeError(f"建仓服务(mcapi)连接失败：{e}（确认 mcapi 已在 {_base()} 运行）")
    if r.status_code >= 400:
        raise RuntimeError(f"建仓服务 {path} 失败 HTTP {r.status_code}: {r.text[:2000]}")
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"建仓服务 {path} 返回非 JSON: {r.text[:200]}")
    if isinstance(body, dict) and "data" in body:
        return body.get("data")
    return body


def wait_operation(operation_id, store=None, timeout=300, interval=3):
    """轮询异步操作到 SUCCESS；FAILED / 超时抛 RuntimeError。返回最终 op dict。"""
    start = time.time()
    while True:
        op = call("GET", f"/operations/{operation_id}", store=store) or {}
        status = op.get("operationStatus")
        if status == "SUCCESS":
            return op
        if status == "FAILED":
            raise RuntimeError(f"建仓操作失败：{op.get('operationProblems') or op}")
        if time.time() - start > timeout:
            raise RuntimeError(f"建仓操作超时（{timeout}s）operationId={operation_id}")
        time.sleep(interval)


def ping():
    """连通性自检：列入库计划（只读）。通则返回计划数，不通抛 RuntimeError。"""
    data = call("GET", "/inbound-plans", params={"page_size": 1}) or {}
    plans = data.get("inboundPlans") or data.get("inboundPlanSummaries") or []
    return {"ok": True, "base": _base(), "sample_count": len(plans)}
