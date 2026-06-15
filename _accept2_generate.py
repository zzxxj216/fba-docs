# -*- coding: utf-8 -*-
"""验收2 生成：四个批次 × 对应模板（与 POST /api/batches/{id}/generate 同代码路径）。"""
import sys

sys.path.insert(0, r"D:\amazon")

from app.database import SessionLocal
from app.models import Batch, Template
from app.services import generate_service

db = SessionLocal()


def tid(name):
    return db.query(Template).filter(Template.name == name).first().id


def bid(name):
    return db.query(Batch).filter(Batch.name == name).first().id


JOBS = [
    ("Serenorch-US-0619", ["跨运通-托书", "跨运通-报关资料", "跨运通-投保",
                           "跨运通-采购合同", "跨运通-9810"]),
    ("HUHOLE-US-0605K", ["新版-跨运通托书(HUBFSE)", "HUHOLE-报关资料",
                         "HUHOLE-投保", "HUHOLE-采购合同"]),
    ("BFPeaky-US-0605", ["新版-跨运通托书(HUBFSE)", "BFPeaky-报关资料",
                         "BFPeaky-投保", "BFPeaky-采购合同"]),
    ("HUHOLE-US-0605Y", ["盈和-托书"]),
]

for bname, tnames in JOBS:
    b = bid(bname)
    ids = [tid(t) for t in tnames]
    print(f"== generate {bname} (batch {b}) templates {ids}")
    res = generate_service.generate(db, b, ids)
    if res.get("blocked"):
        print("  BLOCKED:")
        for e in res["report"]["errors"]:
            print("   ", e["level"], e["msg"])
        continue
    print("  generated:", len(res["generated"]))
    for g in res["generated"]:
        print("   ", g["filename"])
    for e in res["errors"]:
        print("  ERR:", e)
db.close()
print("done")
