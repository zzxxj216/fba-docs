"""MCAPI 赛狐 FBA 建仓客户端。

只封装正向建仓接口；刻意不提供任何 cancel/delete 调用。所有真实写操作仍由
multi-channel-api 的 ``/api/v1/sellfox/inbound/*`` 执行，本机只负责编排与断点记录。
"""

from urllib.parse import quote

import httpx

from .amazon_fba_client import _base, _key


class McapiInboundError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _request(method, path, *, params=None, json=None, timeout=120):
    key = (_key() or "").strip()
    if not key:
        raise McapiInboundError(503, "未配置 MCAPI_KEY，已阻止真实建仓")
    try:
        r = httpx.request(
            method,
            f"{_base()}{path}",
            params=params,
            json=json,
            headers={"X-API-Key": key},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise McapiInboundError(503, f"MCAPI 连接失败：{exc}") from exc
    try:
        body = r.json()
    except ValueError:
        raise McapiInboundError(
            r.status_code or 502,
            f"MCAPI 返回非 JSON：HTTP {r.status_code} {r.text[:300]}",
        )
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("detail") or "")
        raise McapiInboundError(r.status_code or 502, message or str(body)[:500])
    return body.get("data") if isinstance(body, dict) and "data" in body else body


def list_shops():
    data = _request("GET", "/api/v1/sellfox/shops", timeout=60) or {}
    return data.get("shops") if isinstance(data, dict) else data


def claim_build(scope_key, node="build", refs=None):
    return _request(
        "POST",
        "/api/v1/checkpoints",
        json={"scope_key": scope_key, "node": node, "refs": refs or {}},
        timeout=30,
    )


def create_plan(shop_id, name, source_address, items):
    return _request(
        "POST",
        "/api/v1/sellfox/inbound/plans",
        json={
            "shop_id": int(shop_id),
            "name": name,
            "source_address": source_address,
            "items": items,
        },
        timeout=420,
    )


def submit_packing(plan_id, box_specs, owners):
    pid = quote(str(plan_id), safe="")
    return _request(
        "POST",
        f"/api/v1/sellfox/inbound/plans/{pid}/packing",
        json={"box_specs": box_specs, "owners": owners},
        timeout=600,
    )


def list_placements(plan_id, shop_id, regenerate=False):
    pid = quote(str(plan_id), safe="")
    return _request(
        "GET",
        f"/api/v1/sellfox/inbound/plans/{pid}/placements",
        params={"shop_id": int(shop_id), "regenerate": bool(regenerate)},
        timeout=600,
    )


def finalize(plan_id, payload):
    pid = quote(str(plan_id), safe="")
    return _request(
        "POST",
        f"/api/v1/sellfox/inbound/plans/{pid}/finalize",
        json=payload,
        timeout=1200,
    )


def get_plan(plan_id, shop_id):
    pid = quote(str(plan_id), safe="")
    return _request(
        "GET",
        f"/api/v1/sellfox/inbound/plans/{pid}",
        params={"shop_id": int(shop_id)},
        timeout=90,
    )


def get_labels(amazon_shipment_id, print_num=None):
    sid = quote(str(amazon_shipment_id), safe="")
    params = {"print_num": int(print_num)} if print_num is not None else None
    return _request(
        "GET",
        f"/api/v1/sellfox/inbound/shipments/{sid}/labels",
        params=params,
        timeout=180,
    )
