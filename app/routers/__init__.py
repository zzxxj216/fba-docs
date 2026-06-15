"""路由层：每个模块导出 router = APIRouter()，main.py 统一挂 /api 前缀。

集成注意（main.py）：crud.py 含通配路由 /{resource}、/{resource}/{item_id}，
会吞掉 /batches、/shipments/{id} 等具体路径——include_router 时必须把
sync / batches /（以及 Agent B 的 templates / generate）放在 crud 之前，crud 最后挂。
"""
