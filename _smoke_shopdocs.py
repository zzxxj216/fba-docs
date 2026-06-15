"""Smoke test: 店铺 doc_types → suggested_template_ids + 模板网格预览接口。

用 TestClient 直连，不启停服务。测完把 company.doc_types 改回原值，避免污染数据。
注：任务文案写"批次 2"，但本库批次 id 实为 8-13，这里用批次 10（company 8, 货代 1）。
"""

import json

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import Batch, Company, Template

BATCH_ID = 10          # HUHOLE-US-0605K, company_id=8, 货代 {1}
TEMPLATE_ID = 2        # 跨运通-托书（有原件）

client = TestClient(app)


def _internal_and_forwarder_ids(db, forwarder_ids):
    """期望基线：active 且 (internal 或货代命中) 的模板 id 集合。"""
    ids = set()
    for t in db.query(Template).filter(Template.active.is_(True)).all():
        if t.owner_type == "internal" or t.forwarder_id in forwarder_ids:
            ids.add(t.id)
    return ids


def main():
    db = SessionLocal()
    batch = db.get(Batch, BATCH_ID)
    company = batch.company
    forwarder_ids = {s.forwarder_id for s in batch.shipments if s.forwarder_id}
    original_doc_types = company.doc_types  # 备份，结束时还原
    company_id = company.id
    print(f"[setup] batch={BATCH_ID} company={company_id} forwarder_ids={forwarder_ids}")
    print(f"[setup] original doc_types={original_doc_types!r}")

    baseline = _internal_and_forwarder_ids(db, forwarder_ids)
    db.close()

    try:
        # ---- ① 设 doc_types=["托书","采购合同"]，suggested 只含这两类 active 模板
        db = SessionLocal()
        c = db.get(Company, company_id)
        c.doc_types = json.dumps(["托书", "采购合同"], ensure_ascii=False)
        db.commit()
        db.close()

        r = client.get(f"/api/batches/{BATCH_ID}/full")
        assert r.status_code == 200, r.text
        suggested = r.json()["suggested_template_ids"]
        print(f"[case1] doc_types=['托书','采购合同'] -> suggested={suggested}")

        db = SessionLocal()
        types_by_id = {t.id: t.doc_type for t in db.query(Template).all()}
        db.close()
        kinds = {types_by_id[i] for i in suggested}
        assert kinds <= {"托书", "采购合同"}, f"含非清单类型: {kinds}"
        # 必须是 baseline ∩ 该两类
        expect1 = {i for i in baseline if types_by_id[i] in {"托书", "采购合同"}}
        assert set(suggested) == expect1, f"got {set(suggested)} expect {expect1}"
        print(f"[case1] OK kinds={kinds} expect={sorted(expect1)}")

        # ---- ② 清空 doc_types，退化为货代+内部全部 active
        db = SessionLocal()
        c = db.get(Company, company_id)
        c.doc_types = None
        db.commit()
        db.close()

        r = client.get(f"/api/batches/{BATCH_ID}/full")
        suggested2 = set(r.json()["suggested_template_ids"])
        print(f"[case2] doc_types=None -> suggested={sorted(suggested2)}")
        assert suggested2 == baseline, f"got {suggested2} expect {baseline}"
        print(f"[case2] OK == 货代+内部全部 active ({len(baseline)} 个)")

        # ---- ③ GET /api/templates/{id}/grid 多 sheet 网格
        r = client.get(f"/api/templates/{TEMPLATE_ID}/grid")
        assert r.status_code == 200, r.text
        g = r.json()
        print(f"[case3] template {TEMPLATE_ID} '{g['name']}' sheets={len(g['sheets'])}")
        for s in g["sheets"]:
            print(f"        sheet '{s['name']}' rows={s['rows']} cols={s['cols']} "
                  f"cells={len(s['cells'])} merges={len(s['merges'])} "
                  f"truncated={s['truncated']}")
        assert g["sheets"], "无 sheet"
        assert any(s["cells"] for s in g["sheets"]), "全部 sheet 空"

        # ---- ④ POST /api/templates/preview-grid 用 stored_file
        db = SessionLocal()
        stored = db.get(Template, TEMPLATE_ID).stored_file
        db.close()
        r = client.post("/api/templates/preview-grid", json={"stored_file": stored})
        assert r.status_code == 200, r.text
        pg = r.json()
        print(f"[case4] preview-grid stored_file={stored} sheets={len(pg['sheets'])}")
        assert pg["sheets"], "preview-grid 无 sheet"

        # ---- 404 路径
        r = client.get("/api/templates/999999/grid")
        assert r.status_code == 404, r.text
        r = client.post("/api/templates/preview-grid", json={"stored_file": "no_such.xlsx"})
        assert r.status_code == 404, r.text
        print("[case5] 404 paths OK")

        print("\nALL SMOKE TESTS PASSED")
    finally:
        # 还原 doc_types，避免污染数据
        db = SessionLocal()
        c = db.get(Company, company_id)
        c.doc_types = original_doc_types
        db.commit()
        restored = c.doc_types
        db.close()
        print(f"[teardown] restored company {company_id} doc_types={restored!r}")


if __name__ == "__main__":
    main()
