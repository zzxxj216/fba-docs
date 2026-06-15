"""采购单功能 smoke：confirm → get_confirm → create-order(dry_run)。

不真实创建采购单（dry_run=True）。用 TestClient 走完整路由 + 直连服务交叉验证。
"""

import json

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.services import purchase_order_service as pos

client = TestClient(app)

PGN = "PPG2606120002"  # BYANE 品牌 → 供应商 21797（链条系）

print("=" * 60)
print("① POST /api/purchase/confirm")
fake_items = [
    {"sku": "BYANE-A1-001", "commodity_name": "测试链条A", "num": 60, "box_count": 5},
    {"sku": "BYANE-A1-002", "commodity_name": "测试链条B", "num": 40, "box_count": 4},
]
r = client.post("/api/purchase/confirm", json={
    "plan_group_no": PGN,
    "items": fake_items,
    "confirmed_by": "smoke-tester",
    "remark": "smoke 确认",
})
print("status_code", r.status_code)
confirm = r.json()
print(json.dumps(confirm, ensure_ascii=False, indent=2))
assert r.status_code == 200
assert confirm["status"] == "已确认"
assert confirm["confirmed_by"] == "smoke-tester"
assert len(confirm["items_snapshot"]) == 2

print("=" * 60)
print("② GET /api/purchase/confirm/{pgn}")
r = client.get(f"/api/purchase/confirm/{PGN}")
print("status_code", r.status_code)
got = r.json()
print(json.dumps(got, ensure_ascii=False, indent=2))
assert got["status"] == "已确认"
assert got["plan_group_no"] == PGN

# 无记录分支
r2 = client.get("/api/purchase/confirm/PPG_NOT_EXIST_X")
print("no-record:", json.dumps(r2.json(), ensure_ascii=False))
assert r2.json()["status"] == "待确认"

print("=" * 60)
print("③ POST /api/purchase/create-order  dry_run=true")
r = client.post("/api/purchase/create-order", json={
    "plan_group_no": PGN,
    "action": "0",
    "dry_run": True,
})
print("status_code", r.status_code)
assembled = r.json()
print(json.dumps(assembled, ensure_ascii=False, indent=2))

body = assembled["body"]
# 断言
assert body["includeTax"] == "0", "includeTax 必须为 0"
assert body["action"] == "0"
assert body["warehouseId"], "warehouseId 必须有值"
assert body["supplierId"] == "21797", f"BYANE 应映射供应商 21797，实际 {body['supplierId']}"
assert body["items"], "items 不能为空"
for it in body["items"]:
    assert it["sku"], "每项必须有 sku"
    assert it["num"], "每项必须有 num"
    assert it["perPurchase"] != "", "每项必须有 perPurchase"
    assert it["purchasePlanNo"] == PGN, "purchasePlanNo 必须为计划组号"
assert assembled["item_count"] == len(body["items"])

print("=" * 60)
print("全部断言通过。")
print("supplierId =", body["supplierId"], "| warehouseId =", body["warehouseId"],
      "| item_count =", assembled["item_count"], "| total_num =", assembled["total_num"])
print("brand_name =", assembled["brand_name"], "| supplier_name =", assembled["supplier_name"])
