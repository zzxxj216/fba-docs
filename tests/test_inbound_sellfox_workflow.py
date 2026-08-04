import json

import pytest
from fastapi import HTTPException

from app.models import Brand, Product
from app.routers import inbound as router
from app.services import inbound_service as ibs


def _brand_and_product(db):
    brand = Brand(
        name="DemoBrand",
        amazon_store="demo",
        source_address=json.dumps({
            "name": "Sender",
            "address_line1": "No. 1 Road",
            "city": "Hangzhou",
            "state_or_province_code": "Zhejiang",
            "country_code": "CN",
            "postal_code": "310000",
            "phone_number": "10000000000",
        }),
        active=True,
    )
    product = Product(
        sku="SKU-1",
        qty_per_box=10,
        carton_l_cm=40,
        carton_w_cm=30,
        carton_h_cm=20,
        box_weight_kg=12,
        hs_code="test",
        name_customs_cn="test",
        material="test",
        unit_price_default=1,
    )
    db.add_all([brand, product])
    db.commit()
    return brand


def test_resolve_items_requires_exact_carton_quantity(db):
    _brand_and_product(db)
    with pytest.raises(RuntimeError, match="不能被每箱数"):
        ibs._resolve_items(db, [{"msku": "SKU-1", "quantity": 21}])


def test_staged_sellfox_build_pauses_then_finalizes(db, monkeypatch):
    brand = _brand_and_product(db)
    calls = []

    monkeypatch.setattr(router.mcapi, "claim_build", lambda *a, **k: calls.append("claim"))
    monkeypatch.setattr(
        router.mcapi,
        "create_plan",
        lambda *a, **k: {
            "inbound_plan_id": "wf-plan-1",
            "owners": {"SKU-1": {"prepOwner": "NONE", "labelOwner": "SELLER"}},
        },
    )
    monkeypatch.setattr(
        router.mcapi,
        "submit_packing",
        lambda *a, **k: {"packing_option_id": "po-1", "packing_groups": ["pg-1"]},
    )
    monkeypatch.setattr(
        router.mcapi,
        "list_placements",
        lambda *a, **k: [{
            "placement_option_id": "pl-1",
            "shipment_count": 2,
            "fee_usd": 0,
            "fulfillment_centers": [],
            "fc_available": False,
        }],
    )
    finalized = {}

    def _finalize(plan_id, payload):
        finalized.update({"plan_id": plan_id, "payload": payload})
        return {"shipments": [{"amazon_shipment_id": "FBA1", "shipment_id": "sh-1"}]}

    monkeypatch.setattr(router.mcapi, "finalize", _finalize)

    started = router.start_sellfox_build({
        "plan_group_no": "PPG-TEST-1",
        "brand_id": brand.id,
        "shop_id": 123,
        "items": [{"msku": "SKU-1", "quantity": 20}],
    }, db)
    assert calls == ["claim"]
    assert started["requires_selection"] is True
    assert started["record"]["status"] == "分仓方案已生成"
    assert started["record"]["placement_option_id"] in (None, "")

    rec_id = started["record"]["id"]
    done = router.finalize_sellfox_build(rec_id, {
        "placement_option_id": "pl-1",
        "ready_to_ship_start": "2026-08-10T00:00Z",
    }, db)
    assert finalized["plan_id"] == "wf-plan-1"
    assert finalized["payload"]["shipping_mode"] == "FREIGHT_LTL"
    assert finalized["payload"]["carrier_name"] == "Other"
    assert done["record"]["status"] == "运输已锁定"
    assert done["shipments"][0]["amazon_shipment_id"] == "FBA1"


def test_finalize_rejects_unknown_placement_option(db):
    brand = _brand_and_product(db)
    from app.models import InboundPlan

    rec = InboundPlan(
        source_ref="PPG-TEST-2",
        brand_id=brand.id,
        store="123",
        amazon_inbound_plan_id="wf-plan-2",
        shipments_snapshot=json.dumps({
            "placement_options": [{"placement_option_id": "pl-current"}],
        }),
    )
    db.add(rec)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        router.finalize_sellfox_build(rec.id, {
            "placement_option_id": "pl-guessed",
            "ready_to_ship_start": "2026-08-10T00:00Z",
        }, db)
    assert exc.value.status_code == 409
