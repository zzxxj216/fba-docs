# API 契约 & 文件归属（agent 团队共同合同）

> 任何 agent 不得修改不属于自己的文件。公共契约（本文件 + app/models.py + app/database.py + DESIGN.md + CLAUDE.md）只读。

## 文件归属

| 负责方 | 文件 |
|---|---|
| 已完成(只读) | app/database.py, app/models.py, DESIGN.md, CLAUDE.md, .env, requirements.txt |
| Agent A 后端数据 | app/sellfox_client.py, app/services/sync_service.py, app/services/validate_service.py, app/services/import_service.py, app/routers/{crud,sync,batches}.py |
| Agent B 生成引擎 | app/field_registry.py, app/rule_engine.py, app/excel_engine.py, app/services/generate_service.py, app/ai_mapping.py, app/routers/{templates,generate}.py, app/seed.py |
| Agent C 前端 | static/index.html, static/app.js, static/style.css |
| 集成(最后由主线完成) | app/main.py, start.bat |

每个 router 模块导出 `router = APIRouter()`，main.py 统一挂 `/api` 前缀。

## REST 接口

### 通用 CRUD（Agent A，`routers/crud.py`）
资源名: factories, companies, brands, products, forwarders, templates, rule-configs
```
GET    /api/{resource}            列表（全部，含关联名称冗余字段）
POST   /api/{resource}            创建（dict 直传，按模型列过滤）
PUT    /api/{resource}/{id}       更新
DELETE /api/{resource}/{id}       删除（Factory/Company/Brand 被引用时改为停用并返回提示）
```

### 同步（Agent A，`routers/sync.py`）
```
GET  /api/sync/plans?page=1                赛狐 STA 计划列表（透传分页）
POST /api/sync/import {"inbound_plan_id"}  导入/增量更新一个批次 → 返回 batch_id + 校验报告
```
同步规则：edited_fields 里的字段不覆盖；品牌按 shop_name 前缀匹配 Brand.name（忽略大小写），匹配不到批次仍创建但校验报告提示；货代按 logisticProviderName 匹配 Forwarder.sellfox_name。

### 批次（Agent A，`routers/batches.py`）
```
GET  /api/batches/{id}/full       批次+货件+明细(含产品)+逐箱+生成记录+汇总
GET  /api/batches/{id}/validate   校验报告（见下）
PUT  /api/shipments/{id}          编辑货件字段（自动登记 edited_fields）
PUT  /api/shipment-items/{id}     编辑明细字段（同上）
PUT  /api/batches/{id}            编辑批次（contract_date、factory_id 等）
GET  /api/batches/{id}/zip        打包下载该批次全部生成文件
```

校验报告格式（validate_service 产出，generate 前强制调用）：
```json
{"passed": false, "errors": [{"level":"error|warn", "scope":"batch|shipment:3|item:9",
  "field":"hs_code", "msg":"产品 Serenorch-FT-1 缺HS编码", "fix_hint":"products:12"}]}
```
errors 里有 level=error 即 passed=false。检查项：MSKU未匹配产品库、产品缺必填(报关名/HS/材质/申报要素)、单价/箱数/数量为空、Σ明细数量≠货件quantity、批次缺品牌/主体/工厂绑定、报关需英文抬头而主体缺英文名。

### 模板与生成（Agent B，`routers/templates.py` `routers/generate.py`）
```
POST /api/templates/upload-file        (multipart) → {stored_file, original_name, sheetnames}
POST /api/templates/ai-mapping         {"stored_file"} → AI 映射草稿 {mapping, confidences, notes}
POST /api/templates/{id}/test-generate {"batch_id"} → 单模板试生成（不落 GeneratedDoc）→ 文件下载
POST /api/batches/{id}/generate        {"template_ids":[...]} → 强制先校验，passed 才生成
                                       → {"generated":[{doc_id,filename,shipment_id}], "errors":[...]}
GET  /api/docs/{doc_id}/download
DELETE /api/docs/{doc_id}
GET  /api/fields                       字段字典（前端映射配置面板用）
POST /api/seed/init                    初始化：规则默认值 + 工厂信息库.xlsx 导入 + 产品列表导入(可选文件)
```

generate 行为：granularity=shipment 时每货件出一份（文件落 output/{批次名}/{FC}/）；batch 时一份（落 output/{批次名}/）；row_per_shipment 时单文件内每货件一行。requires_forwarder_no=True 的模板，对缺货代单号的货件跳过并在 errors 里说明。

### 导入（Agent A，import_service + crud 路由内）
```
POST /api/products/import     (multipart) 兼容"商品列表-报关"表头 → {created, updated}
POST /api/brands/import       (multipart) "店铺主体-工厂信息库.xlsx"格式：
                              自动建 Factory/Company(shop)/Brand + 星盟 sheet → Company(trade)
GET  /api/products/import-template     下载产品导入空模板
```

## 字段字典命名空间（Agent B 在 field_registry.py 落地，前端照此展示）

`batch.*`(name/brand/country/contract_date/base_date) · `shipment.*`(amazon_shipment_id/reference_id/ship_sn/fc_code/address_line1/city/state/postal_code/carton_num/total_gross_weight/total_net_weight/total_volume/total_qty/total_value/forwarder_order_no) · `item.*`(seq/msku/fnsku/5种品名/hs_code/declare_elements/material/usage/brand/model/qty/box_count/qty_per_box/carton尺寸/customs_unit_price/amount/image_url) · `box.*`(box_no/box_id/msku/qty/尺寸in+cm/重量lb+kg) · `company.* factory.* trade.* forwarder.*` · `calc.*`(doc_no/purchase_no/price_vat/amount_vat/price_vat_markup/amount_vat_markup/insurance_price/insurance_amount/contract_date_cn 等规则引擎产物) · `meta.*`(today/base_friday多格式)

取值统一走 `field_registry.build_context(batch, shipment=None) -> dict`，规则计算统一走 `rule_engine`。

## 映射 JSON 格式（DESIGN.md §6 为准）

sheets[].cells{addr: path} / sheets[].anchors[{find,in_column,offset_col,row_offset?,value}] / sheets[].tables[{source:"items|boxes|shipments", start_row, insert_rows, columns{col:path}}]，`text:` 前缀=固定文字，含 `{}`=格式串。granularity 与 filename 在 Template 行上（不在 mapping 里）。

## 前端（Agent C）

Vue3 CDN（jsdelivr）单页，7 个导航：批次管理(列表+详情含核对/生成/回填) / 产品库 / 工厂管理 / 主体与品牌(3标签) / 货代&模板(3步向导+AI助手+字段字典面板) / 规则配置 / 生成记录。交互要求见 DESIGN.md §7-8 与会话确认的草图。所有请求走上述 REST；校验未通过时生成按钮禁用并展示错误清单（带"去修复"跳转）。
