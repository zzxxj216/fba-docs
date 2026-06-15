# 验收报告 2 —— HUBFSE 新测试数据（盈和 + 跨运通双货代）逐单元格 diff

> 日期：2026-06-12 · 验收人：验收工程师（Claude）
> 数据：`C:\Users\zane\Downloads\HUBFSE自动化\`（BFPeaky / HUHOLE / Serenorch 共 16 个批次文件夹）
> 方法：沿用 P2（`P2_DIFF_REPORT.md`）——离线导入 → 配置/映射 → 生成 → 与 RPA 成品逐单元格 diff → 迭代到我方错误 = 0
> diff 脚本：`D:\amazon\_accept2_diff.py`（可重复执行；五票全量约 13 万格）

## 结论

**五票 60 个生成文件全部通过：我方错误 = 0**（共比对 **134,893** 个单元格），盈和与跨运通两家货代、三个品牌（Serenorch 复验 + HUHOLE/BFPeaky 新建）全覆盖。

| 票 | 案例 | 货代 | 文件 | 比对格数 | MATCH | 日期类 | 数据缺口 | RPA bug | **我方错误** |
|---|---|---|---|---|---|---|---|---|---|
| SE | Serenorch-US-6.19（精验，复验既有模板 id2-6） | 跨运通 | 17 | 47,415 | 47,345 | 15 | 45 | 10 | **0** |
| HUK | HUHOLE-US-6.5 跨运通（精验，新品牌） | 跨运通 | 16 | 43,248 | 43,243 | 0 | 0 | 5 | **0** |
| HUY | HUHOLE-US-6.5 盈和（**盈和精验票**） | 盈和 | 5 | 415 | 415 | 0 | 0 | 0 | **0** |
| BF | BFPeaky-US-6.5（抽查，新品牌） | 跨运通 | 15 | 39,210 | 39,205 | 0 | 0 | 5 | **0** |
| HU12 | HUHOLE-US-6.12（抽查，加测投保仓库货值） | 跨运通 | 7 | 4,605 | 4,605 | 0 | 0 | 0 | **0** |

生成路径：`D:\amazon\output\{Serenorch-US-0619, HUHOLE-US-0605K, HUHOLE-US-0605Y, BFPeaky-US-0605, HUHOLE-US-0612K}\`（货件级在 `{FC}\` 子目录）。

### 迭代记录

- **第 0 轮（离线预验）**：先用独立引擎测试脚本拿成品反推数据直接灌引擎（不经 DB），对 HU 报关资料/盈和托书/HU 跨运通托书逐格预验——插行+公式平移引擎一次通过（0 差异），盈和托书 83 格、跨运通托书 5,660 格全对。
- **第 1 轮（全链路）**：我方错误 105 处，全部为**映射取数口径**问题（引擎零问题）：
  1. SE 托书 G 列实际取「中文品名(用途功能)」而非中文报关名、I 列为固定 `'2'` 而非申报单价——P2 时代单 SKU 批次两列恰好同值，6.19 双 SKU 批次（CT-30 报关价 1.2≠2、品名不同）暴露真口径（×5 文件 ×3 格）；
  2. SE 9810 L 列 = 货件内 SKU 序号（P2 单 SKU 恒 1 被误配为常量）（×5 行）；
  3. HU/BF 报关资料 发票 G2、箱单 G3/G4/G5：模板原为跨 sheet 公式，RPA 成品覆写为字面值，改为映射显式写值（×10 文件 ×4 格）。
- **第 2 轮**：四票（SE/HUK/HUY/BF）我方错误 = 0。
- **第 3 轮（加测 HU12 暴露）**：采购合同聚合行序=**当周**补仓计划行序，而非产品建档序（同一 SKU 每周行位变化）→ 新增 `Product.sort_index`（每次产品导入写入行序），`items_agg_by_product` 按其排序；重生成后收敛，HU12 我方错误 = 0。

---

## 一、逐票结果与差异定性

### 1.1 SE = Serenorch-US-6.19（跨运通复验票，既有模板 id2~6）

源数据：`发货单-20260611-*.xlsx`（旧版"发货单导出"格式）+ 6.19 版`商品列表-报关xlsx.xlsx`（重导，updated 23）。
17 个文件 = 托书×5 + 报关资料×5 + 投保×5 + 采购合同×1 + 9810×1。**采购合同 90 格 100% MATCH**。

非 MATCH 差异（70 格）逐项定性：

| 类别 | 格 | 说明 |
|---|---|---|
| 日期类 15 | 9810 B 列×10 | 报送时间=生成时刻（RPA 跑批 20260612093500，我方为本次生成时间戳） |
| | 投保 U3×5 | 起运日期：RPA 写跑批当天的周五 2026-06-12，我方写发货基准周五 2026/06/19（RPA 的"起运日期"语义是跑批周，复现需回拨系统时钟，不可取） |
| 数据缺口 45 | 托书 H18/H19（英文报关名）、L18/L19+报关单 B20/B23（海关编码 090↔010）、M18/M19（分类）、F18（FT-1 箱规高 44↔77cm）×5 | **商品主数据版本漂移**（与 P2 完全同组字段）：RPA 跑批所用商品列表 ≠ 案例文件夹随附的 6.19 版商品列表。本系统按现行主数据生成（主数据即真相）；把这几个字段改回跑批值即可全绿 |
| RPA bug 10 | 报关单 D21/D24×5 | 申报要素行 RPA 写入未生效（成品为空），本系统按映射补全 `item.declare_full`——与 P2 第 2 类 bug 相同 |

老 RPA 验证要点（全 MATCH）：多 SKU 批次首次覆盖——逐 SKU 箱数 R18=13/R19=2（装箱信息一票两行按详情行序对齐）、报关单两明细块（20-22/23-25）、合同/发票/箱单**插行后预留行下移成尾部空格式行 + SUM 区间扩张**（`=SUM(C8:C9)→=SUM(C8:C10)`、`=箱单!F10→=箱单!F11`）、9810 十行（5 货件×2 SKU，L 列 1/2 交替、H 列=货件总金额 720=312×2+80×1.2）、合同日期=基准周五−30 天（06-19→05-20，编号 SE-ABE8-2026-05-20）、投保 AH3=Σ数量×采购成本。

### 1.2 HUK = HUHOLE-US-6.5 跨运通（新品牌精验票）

源数据：`FBA货件-920336340792705024.xlsx`（**新版导出**：货件详情/装箱明细，自带完整地址+逐 SKU 箱规英寸）+ `补仓计划-US-6.5-跨运通.xlsx`（产品主数据，18 SKU）+ `HU2026报关价格试算.xlsx` sheet `6.5HU-US`（当周报关价，18/18 命中成品）。
16 个文件 = 托书×5（运单信息模板）+ 报关资料×5 + 投保×5 + 采购合同×1。

| 类别 | 格 | 说明 |
|---|---|---|
| RPA bug 5 | 投保 U3×5 | 必填"起运日期"RPA 写入未生效成品为空；我方按基准周五补全（同 P2 第 3 类 bug） |
| 其余 43,243 格全部 MATCH | | |

验证要点（全 MATCH）：托书 C 列=箱规长×宽×高(in)乘积全精度（16.93×9.45×21.06=3369.35781）、H7 组合地址串（`GYR2  -  17341 W MINNEZONA AVE  -  GOODYEAR  -  AZ`）、J 列恒 2；报关单合同号 `BY-SA-HU-{FC}-2026-05-05`（合同日期=上月同日）、22 明细块插行+`=箱单!C30` 跨 sheet 公式平移、箱单/合同/发票合计行 RPA 覆写字面值（51/546/804.97/779.47）；投保 W3 `车库挂钩+512.0pcs`（**RPA 浮点尾巴按 total_qty_float 复刻**）、Z3=Σ箱数×单箱重、AH3=Σ数量×成本×1.695 全精度（15618.93345）；采购合同行序=补仓计划行序（items_agg_by_product）、单价=成本×1.695 不舍入（36.62895）、模板 2 预留行插 17 行后尾部留 1 空格式行、交货时间行锚点落位 A39。

### 1.3 HUY = HUHOLE-US-6.5 盈和（盈和精验票）

盈和侧 RPA 成品**只有托书**（报关/投保/采购合同成品只覆盖跨运通货件——盈和渠道含报关投保服务；全部 16 个案例文件夹均如此）。5 份盈和托书 415 格 **100% MATCH（0 差异）**。

- 模板：案例中无空白盈和托书 → 从成品复制清掉数据格制成 `accept2_盈和托书-空白模板.xlsx`（挂货代"盈和国际物流"）。
- 锚点行式映射：D 列找 `FBA仓库代码*` 行 → E=FC；找 `客户单号*` 行 → E/L=FBA 号、M=Reference ID、N=总箱数；明细 @23 行：B=箱数 C/D=中英品名 E=**采购价** F=数量 G/H=中英材质（新增 `Product.material_en`）J=SKU R=海关编码 U/V/W=`否` X=0。
- 源数据缺口：所有 FBA货件 导出均不含盈和货件的 FBA 号 → 批次按 盈和补仓计划（产品主数据）+ 货代标 PDF 文件名（FC↔FBA 号）+ 成品托书（RefID、逐 SKU 箱数拆分）回填重建，回填范围仅限"数据身份字段"，全部计算字段（数量=箱数×每箱数、合计等）由系统规则产出后与成品比对。

### 1.4 BF = BFPeaky-US-6.5（新品牌抽查票）

源数据同 HUK 结构（FBA货件-920329846391103488 + 6.5-US-补仓计划 + BF报关价格试算 `6.5BF-US`，4 SKU）。15 个文件 = 托书×5 + 报关资料×5 + 投保×5。**该周无采购合同 xlsx 成品**（转账资料里只有 PDF），BFPeaky-采购合同模板已配好（买方=星盟，映射 trade.*，与 0508 成品 `SA-BF-2026-04-08` 口径核对）。
差异仅投保 U3×5（RPA bug 同上），其余 39,205 格全 MATCH，含报关合同号 `BY-SA-BF-{FC}-2026-05-05`、投保 `车库挂钩+54.0pcs`、AH3=Σ×1.695（3307.962）。

### 1.5 HU12 = HUHOLE-US-6.12（加测：投保仓库货值）

7 个文件 = 报关资料×5 + 采购合同×1 + 投保仓库货值×1，**4,605 格 100% MATCH（0 差异）**。
产品按当周口径刷新（补仓计划-US-6.12-跨运通 + 试算 `6.12HU-US`，12 SKU），验证了"报关价/品名/行序随周更新"的运转方式；投保仓库货值（模板 id15，row_per_shipment）11 个值格（品牌/各 FC 货值=Σ数量×成本×1.695/FBA 号）全对。

范围说明：
- **投保单不生成**：6.12 成品投保的 D3/F3/G3（货代单号）为空——RPA 在货代单号签发前就出了投保文件；本系统 `requires_forwarder_no=True` 为 P2 拍板的强制约束（缺单号跳过并提示回填），属设计差异而非缺陷。
- **托书不在范围**：6.12 托书是又一变体模板（sheet=发票模板/Sheet1，文件名 `HUHOLE-US-{FC}-06-12-托书`），案例中无空白模板且无第二周同款成品可交叉验证，单独建模板收益有限；如业务需要按本报告模板配置方法 10 分钟可补。
- **投保仓库货值行序**：成品行序为 RPA 任意序（LGB8,LAS1,GYR2,TOL3,MQJ1），我方固定 FC 字典序输出（可重现）；diff 按 FC 配对比值。

---

## 二、RPA bug 汇总（本系统输出为正确一方）

1. **投保"起运日期"U3 漏写**（HUK×5 + BF×5）：模板必填项，RPA 写入未生效成品为空；本系统填基准周五。与 P2 完全相同的缺陷在新品牌流程中仍存在。
2. **报关单申报要素行写入失败**（SE×10：D21/D24）：成品报关单没有"用途|材质|品牌"申报要素；本系统补全。
3. （记录在案、不计差异）RPA 浮点痕迹：HUHOLE/BFPeaky 投保 W3 货物描述把整数数量写成 `512.0pcs`；本系统以 `shipment.total_qty_float` 复刻成品口径，新批次可改回整数格式串。

## 三、数据缺口与回填记录

| 缺口 | 处理 |
|---|---|
| 盈和批次无源发货单（FBA货件导出不含盈和货件） | 按 盈和补仓计划+货代标PDF+成品托书 回填重建（批次 `HUHOLE-US-0605Y`，shipments.remark 标注），见 §1.3 |
| 旧版发货单导出无收件地址 | SE 6.19 沿用 P2 手段：托书模板「地址库」sheet 按 FC 回填（5 仓全 MATCH） |
| 货代单号 | 按货代标 PDF 文件名回填：SE XX266-270、HUK XX209-213、BF XX204-208（PDF 名中的箱数与货件箱数互验一致） |
| 产品图片 | SE 由 6.19 成品托书 O/Q 列回填 image_url（发货单导出含图片列、离线文件未存——同 P2） |
| 批次日期 | SE：base=2026-06-19、contract=2026-05-20（RPA 口径=基准周五−30 天，同 P2 需人工指定）；HUHOLE/BFPeaky：contract=上月同日（06-05→05-05、06-12→05-12），**与系统默认规则一致，无需人工干预** |
| SE 商品主数据漂移（45 格） | 不可修复、按 P2 口径说明：RPA 跑批 master ≠ 案例随附 6.19 版商品列表（英文报关名/海关编码 090↔010/分类/FT-1 箱规高 44↔77）。本系统按现行主数据生成 |

## 四、环境问题记录（影响验收方式，不影响结论）

- **8000 端口服务（--reload）对所有 HTTP 请求无响应**（连接建立不返回；netstat：PID 34592 多个 ESTABLISHED 滞留）。按约定未启停。根因定位：本机 python 进程**首次 import sqlalchemy 耗时 ~20 分钟**（`python -v` 显示卡在 socket/原生扩展加载段，疑似安全软件实时扫描；worker 实测 1220.6s），--reload 在代码变更后重导入时长时间阻塞 → 服务事件循环挂起。
- 应对：起常驻 worker 进程（`D:\amazon\_accept2_worker.py`，一次导入 + 命令文件派发 + 模块热重载），所有导入/生成通过它执行——**与 `POST /api/sync/import-excel`、`POST /api/batches/{id}/generate` 完全同一 service 代码路径**（P2 同款做法），校验前置照常生效（四个批次 validate passed=True 后才生成）。服务恢复后全程可走 API。
- 赛狐 API 未调用（全部离线导入），1次/秒限流无关。

## 五、新增配置清单

### 5.1 主数据（全部经 ORM upsert，幂等脚本 `_accept2_setup.py`）

| 对象 | 内容 |
|---|---|
| Company 杭州舟峰科技有限公司（id=8,shop） | HUHOLE 境内发货人/被保险人/采购合同买方；USCC 91330114MA2KL9FL58；中英文名/地址/电话齐；`export_via_trade=True`、`insurance_factor=1.695` |
| Company 杭州保峰五金制造有限公司（id=9,shop） | BFPeaky 主体；USCC 91330109793691699K（与同名工厂不同证照号）；`export_via_trade=True` |
| Factory 杭州保峰五金制造有限公司（id=5） | HUHOLE/BFPeaky 共同工厂（采购合同卖方）：杭州市萧山区义桥镇新坝村 / 13758258357 |
| Brand HUHOLE（id=7,HU） | factory=保峰、company=舟峰；`doc_no_rule_shipment='BY-{sa}{brand2}-{fc}-{date}'`、`doc_no_rule_purchase='{sa}{brand2}-{date}'`（成品 BY-SA-HU-LAS1-2026-04-29 / SA-HU-2026-05-05 反推） |
| Brand BFPeaky（id=8,BF） | factory=保峰、company=保峰公司；编号规则同上（BY-SA-BF-…） |
| Company 星盟（trade） | 补地址/电话（BFPeaky 采购合同买方=星盟，模板 B9/B11 对应） |
| RuleConfig `contract_price_factor` | global=1.0；company:8=1.695；company:9=1.695（=1.5×1.13，**全精度不舍入**，由成品 21.61×1.695=36.62895 实证） |
| Forwarder 盈和 | 已存在（id=3 盈和国际物流，sellfox_name=盈和国际物流，用户预置未改动） |
| Product +24 | HUHOLE 18（6.5 补仓计划+试算 6.5HU-US）+ 盈和 2（GS/PS-Black-12，含新列 material_en='Wood + Steel'）+ BFPeaky 4；**报关单价随周更新**：导入批次前刷新当周试算 sheet（6.12 加测时已演示 6.12HU-US 刷新，明细行价格在导入时落库、不受后续刷新影响） |

### 5.2 新模板（id7~15，文件入 `templates_store/accept2_*`）

| id | 名称 | 粒度/归属 | 映射要点 |
|---|---|---|---|
| 7 | 新版-跨运通托书(HUBFSE) | shipment/跨运通 | sheet`运单信息`：F3=carton_num，M6=postal，H7=`{fc}  -  {addr1}  -  {city}  -  {state}`，H8/M8/M9；items@12（容量 23 行不插行）：A/B=FBA号/Ref，C=`item.carton_lwh_in`(长×宽×高 in 乘积)，D=箱数，E=单箱重，F=HS，G/H=中英品名，I=每箱数，J=`num:2`，K=品牌，L=MSKU，M=材质，N=用途，O=图片；文件名 `{brand}-普船-{fc}-{mm-dd}-托书`（成品日期为人工即时日期 06-02，仅文件名差异） |
| 8 | 盈和-托书 | shipment/盈和 | 锚点行式+items@23，见 §1.3；文件名 `{brand}-普船-{fc}-{mm.dd}-托书` |
| 9/12 | HUHOLE/BFPeaky-报关资料 | shipment/跨运通 | 同构跨运通报关 5 sheet；cells：报关单 A10=calc.doc_no、K4=基准周五，合同 F8、发票 G2=calc.doc_no、G3，箱单 G3/G4/G5 显式写值（RPA 覆写模板公式）；tables 全部 `insert_rows`（报关单 step3：A/B/D/D+1=declare_elements/G/I）；anchors 写合计行字面值（合同`总值`G、发票`TOTAL:`H、箱单`合计`C/D/F/G=箱数/数量/毛重/净重）；境内发货人=模板预填（舟峰/保峰），换主体需补 C3/A4/C7/A8 映射 |
| 10/13 | HUHOLE/BFPeaky-投保 | shipment，requires_forwarder_no | D3/F3/G3=货代单号，S3=FC，U3=基准周五，W3=`车库挂钩+{total_qty_float}pcs`，Y3=箱数，Z3=总毛重，AH3=`shipment.total_contract_value`（Σ数量×成本×1.695 不舍入）；A3 等为模板预填 |
| 11/14 | HUHOLE/BFPeaky-采购合同 | batch/internal | B2/B3/B5=工厂三行；买方 HUHOLE=company.*、**BFPeaky=trade.*（星盟）**；F5=calc.purchase_no、F7=合同日期；items_agg_by_product@17 insert（行序=补仓计划行序）：A=中文品名 C=Σ数量 E=`calc.price_contract`；锚点：`(9)付款方式` 行上 2 行写 `(7) 交货时间：{基准周五}` |
| 15 | HUBFSE-投保仓库货值 | row_per_shipment/internal | 模板由成品清数据制成；A2=品牌，rows@2：B=FC C=`total_contract_value` D=FBA号；我方行序=FC 字典序（成品为 RPA 任意序） |

### 5.3 既有模板修订（id2/3/5/6，复验中按成品真值修正）

| 模板 | 修订 | 依据 |
|---|---|---|
| id2 跨运通-托书 | G 列 `item.name_customs_cn`→`item.name_usage`；I 列 `item.customs_unit_price`→`text:2` | 6.19 双 SKU 成品：G='压缩毛巾30pc'（用途功能品名）、I='2'（CT-30 报关价 1.2 仍写 2）。P2 单 SKU 两口径同值未暴露 |
| id3 跨运通-报关资料 | 各明细区 `reserved_rows` 删行口径 → `insert_rows` 插行口径 | 6.12 后 RPA 升级：永远插 n−1 行、预留行下移成尾部空格式行、SUM 区间扩张（6.19 成品 `=SUM(C8:C10)`、合计行 11）。**P2 老成品（删行口径）与新成品不能同时满足，按最新成品为准** |
| id5 跨运通-采购合同 | 同上插行口径；删除写死坐标 A27 的交货时间，改锚点（`(9)付款方式` 上一行） | 6.19 成品交货时间覆写在 ' Total Value:' 行位（随明细行数浮动） |
| id6 跨运通-9810 | L 列 `num:1`→`item.seq`；H 列 `item.amount`→`shipment.total_value` | 6.19 双 SKU 成品 L=1/2 交替、H=货件总金额 720（P2 单 SKU 恰好相等掩盖） |

### 5.4 引擎/服务扩展（代码改动，`--reload` 服务恢复后自动生效）

| 文件 | 改动 |
|---|---|
| `app/excel_engine.py` | ①插行模式重写：`_replicate_block` 整块复制（值+公式相对平移+样式+合并+行高），插入点=首块之后，复刻 RPA"插 n−1 行"语义（模板多预留行自然下移成尾部空行）；②`_adjust_formulas_insert`：插行后全工作簿公式按 Excel 语义平移/区间扩张（含跨 sheet `=箱单!C9→C30`）；③`_insert_rows_keep_merges`；④`fill` 顺序 cells→tables→anchors（锚点可定位插/删行后的合计行） |
| `app/field_registry.py` | 新字段：`item.material_en/carton_lwh_in/product_id/product_sort`、`calc.price_contract/amount_contract`（不舍入）、`shipment.total_contract_value/total_qty_float`、`meta.base_friday_mm_dot_dd`；新批次级数据源 `items_agg_by_product`（按 `Product.sort_index`=当周计划行序聚合排序，缺省回退 product_id；**注意**：重生成历史批次采购合同前需先刷新当周产品表——与"报关价随周更新"同一口径） |
| `app/rule_engine.py` | 规则 `contract_price_factor`（主体级覆盖）+ `price_contract()`（不四舍五入） |
| `app/services/import_service.py` | ①新版「FBA货件」导出导入（货件详情/装箱明细：完整地址、逐 SKU 箱规 in→cm、逐 SKU 箱数/每箱数）；②旧版装箱信息一票多行：按详情行序对齐逐 SKU 箱数（6.19 实证 13/2↔FT-1/CT-30，托书 R 列一致）；③read_only 模式 `reset_dimensions()`（部分赛狐导出 dimension 元数据损坏，只读出 1 列）；④产品导入（含商品列表-报关）写 `sort_index` 行序 |
| `app/models.py` | `Product.material_en`、`Product.sort_index`（MySQL ALTER 均已执行） |

## 六、未尽事项（超出本轮精验+抽查范围，均有既定接入路径）

- 其余周批次（各品牌 5.1/5.8/5.15/5.22/5.29）：同 `_accept2_batches.py` 手段（先刷当周产品→导入→生成）即可批量复验；本机 python 导入异常缓慢（见 §四）使单轮迭代成本约 25 分钟，本轮按任务要求做满"1 盈和精验 + 1 跨运通精验 + 2 票抽查 + 1 票加测"五票。
- HUHOLE/BFPeaky 6.12 周托书变体模板（发票模板/Sheet1 结构）与无货代单号的 6.12 投保：见 §1.5 范围说明。
- `Serenorch-US-6.12 UK CA` 文件夹：UK/CA 国别案例（非 US 链路，9810/报关国别参数不同），建议另立验收轮次。
- P2 老成品（5.29 删行口径）与 6.12 后新成品（插行口径）不能由同一套映射同时复现（RPA 行为变更），本轮以最新成品为准；若需回放 P2 仅需把 id3/id5 的 `insert_rows` 改回 `reserved_rows`。

## 七、复核方式

```bash
python D:\amazon\_accept2_diff.py            # 五票全量
python D:\amazon\_accept2_diff.py SE HUY     # 指定票
# 重新生成（服务恢复后走 API；当前可经 worker：）
#   POST /api/batches/{9,10,11,12,13}/generate  （批次 id：SE=9 HUK=10 BF=11 HUY=12 HU12=13）
```

辅助脚本（保留备查）：`_accept2_setup.py`（主数据/产品/模板）、`_accept2_batches.py`（四票导入+回填）、`_accept2_generate.py`、`_accept2_bonus612.py`、`_accept2_worker.py`（慢导入环境的常驻执行器）、`_accept2_notes.md`（探查笔记）。
