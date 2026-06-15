# 验收2 工作笔记（草稿，最终写入 ACCEPTANCE_REPORT2.md）

## 环境异常记录
- 8000 端口服务（--reload）对所有 HTTP 请求无响应（连接建立但不返回，含静态页）。
  netstat 显示 PID 34592 LISTENING，多个 ESTABLISHED 滞留。按约定不重启。
  根因推测：本机 sqlalchemy（原生扩展）首次导入耗时 ~20 分钟（疑似安全软件扫描），
  --reload 在代码变更后重新导入时长时间阻塞 → 服务挂起。
  验收改用 P2 同款直连路径（service 函数 = API 同代码路径），并起常驻 worker
  进程（D:\amazon\_accept2_worker.py）一次导入、命令文件派发任务。
- 赛狐 API 未触碰（离线导入），限流无关。

## 案例结构结论（探查）
- Serenorch-* 批次 = 老 RPA（1c268d8a）旧链路：发货单导出（发货单详情/装箱信息）
  + 商品列表-报关 + 托书-空白模板(模板/地址库)/报关模板/投保模板/采购合同模板/9810模板。
- HUHOLE/BFPeaky = 新 RPA 工具链：源数据为「FBA货件-*.xlsx」（货件详情/装箱明细，
  含完整地址+逐SKU箱规英寸）+ 补仓计划（产品主数据）+ {HU,BF}报关价格试算（**每周
  一个 sheet 的报关价**）。模板：拖书模板(跨运通下单模板/运单信息)、HU/BF报关模板(新)、
  投保模板、工厂-舟峰-采购合同模板/BF采购合同模板。
- 盈和 vs 跨运通：同一品牌同一周可拆两票（…-盈和 / …-跨运通 子文件夹）。
  **盈和侧 RPA 成品只有托书**（发票模板/锚点行式）；报关/投保/采购合同成品只覆盖
  跨运通货件（盈和渠道含报关投保服务）。盈和托书的源发货单未保留在案例中
  （所有 FBA货件导出均不含盈和 FBA 号）→ 盈和批次按 补仓计划+货代标PDF+成品托书
  回填重建（数据缺口，报告说明）。
- 6.19 起（及 HUHOLE/BFPeaky 全部）RPA 升级为"插行"口径：明细区永远在首块后
  插 n-1 行，模板多余预留行下移成尾部空格式行；公式区间随插行扩张
  （P2 时代是"删行"口径）。模板 id3/id5 映射已切换为 insert_rows。

## 主数据反推
- HUHOLE：境内发货人/被保险人=杭州舟峰科技有限公司（91330114MA2KL9FL58），
  工厂=杭州保峰五金制造有限公司（采购合同卖方），买方=舟峰；
  报关合同号 BY-SA-HU-{FC}-{合同日期}、采购合同号 SA-HU-{合同日期}
  → Brand.doc_no_rule_shipment='BY-{sa}{brand2}-{fc}-{date}'，purchase='{sa}{brand2}-{date}'，
  舟峰.export_via_trade=True（{sa}='SA-'）。
- BFPeaky：主体=杭州保峰五金制造有限公司（91330109793691699K，与工厂同名另立公司行），
  采购合同卖方=保峰工厂、买方=**星盟**（映射 B7/B9/B11 → trade.*）。
  报关合同号 BY-SA-BF-{FC}-{date}。
- 合同/投保价系数：HUHOLE/BFPeaky 合同单价=采购成本×1.695（=1.5×1.13，全精度不舍入，
  成品 21.61×1.695=36.62895），投保货值=Σ数量×成本×1.695；Serenorch=×1.0。
  → RuleConfig contract_price_factor（global=1.0，company:舟峰=1.695，company:保峰=1.695），
  新 calc.price_contract / shipment.total_contract_value（不舍入）。
- 申报单价（报关）按周变化：取 {HU,BF}报关价格试算 当周 sheet（6.5HU-US 18/18 命中
  0605 成品报关价）→ 导入批次前刷新 Product.unit_price_default。
- 新托书 J 列单价恒 2（补仓计划"单价"列），映射用 num:2 字面量。

## 日期口径
- HUHOLE/BFPeaky：合同日期=基准周五的上月同日（06-05→05-05 ✓ 与系统默认规则一致）。
- Serenorch 6.19：合同日期=基准周五−30天（06-19→05-20，与 P2 相同的 RPA 口径），
  批次上人工指定。
- 投保"起运日期"U3：SE 6.19 成品=RPA 跑批当天(2026-06-12，周五)；HU/BF 成品=空
  （RPA 写入未生效，同 P2 bug）。我方统一写基准周五。

## 引擎扩展（本轮代码改动）
- excel_engine：①插行模式重写——整块复制（值+公式相对平移+样式+合并+行高），
  插入点=首块之后；②_adjust_formulas_insert：全工作簿公式按 Excel 语义平移/区间扩张
  （含跨 sheet =箱单!C9→C30）；③_insert_rows_keep_merges；④fill 顺序改为
  cells→tables→anchors（锚点定位插/删行后的合计行/付款方式行）。
- field_registry：item.material_en/carton_lwh_in/product_id、calc.price_contract/amount_contract、
  shipment.total_contract_value/total_qty_float、meta.base_friday_mm_dot_dd、
  数据源 items_agg_by_product（按产品主数据行序聚合=补仓计划行序=RPA 采购合同行序）。
- rule_engine：contract_price_factor 规则 + price_contract()（不舍入）。
- import_service：①新版「FBA货件」导出导入（货件详情/装箱明细，地址/逐SKU箱规/箱数）；
  ②旧版装箱信息一票多行（按详情行序对齐逐SKU箱数，6.19 案例核实 13/2↔FT-1/CT-30）；
  ③read_only 维度损坏 reset_dimensions。
- models：Product.material_en（ALTER TABLE 已执行）。
- 9810 映射 H 列改 shipment.total_value（多 SKU 时 H=货件总金额≠行金额，P2 单 SKU 恰好相等）。

## 已知/预期差异分类
- SE 9810 B 列报送时间=生成时刻（日期类）。
- SE 投保 U3（日期类：RPA 写跑批周五 06-12，我方基准周五 06-19）。
- HU/BF 投保 U3 成品为空（RPA bug，同 P2）。
- SE 报关单 D21/D24 申报要素 RPA 写入未生效（RPA bug，同 P2）。
- 托书文件名日期：跨运通成品 06-02（人工/RPA 即时日期），我方 06-05 基准周五（仅文件名）。
- HU 投保 W3 '…+512.0pcs' 浮点尾巴：我方按 total_qty_float 复刻。
