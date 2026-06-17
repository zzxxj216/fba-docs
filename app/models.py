"""数据模型 —— 全系统的契约，字段含义见 DESIGN.md §3。

MySQL 注意：String 必须带长度；金额/重量用 Float 即可（本系统非财务核算）。
edited_fields：JSON 数组，记录人工改过的字段名，重新同步赛狐时这些字段不覆盖。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from .database import Base


# ---------------------------------------------------------------- 主数据

class Factory(Base):
    """工厂（独立模块）。"""
    __tablename__ = "factories"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), default="")
    abbr = Column(String(16), default="")          # LG/YY/ZE…
    address = Column(String(255), default="")
    phone = Column(String(64), default="")
    contact = Column(String(64), default="")
    tax_no = Column(String(64), default="")
    bank_info = Column(Text)
    remark = Column(Text)
    active = Column(Boolean, default=True)         # 删除保护：被引用过的只停用


class Company(Base):
    """公司主体：店铺公司(shop) / 外贸主体(trade，星盟)。"""
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    type = Column(String(16), default="shop")      # shop / trade
    name_cn = Column(String(128), default="")
    name_en = Column(String(255), default="")
    address_cn = Column(String(255), default="")
    address_en = Column(String(255), default="")
    abbr = Column(String(16), default="")          # 星盟=SA
    uscc = Column(String(64), default="")          # 统一社会信用代码
    customs_code = Column(String(32), default="")
    phone = Column(String(64), default="")
    contact = Column(String(64), default="")
    bank_info = Column(Text)
    insurance_factor = Column(Float, default=1.0)  # 保价倍率：1.65 / 1.13 / 1.0
    export_via_trade = Column(Boolean, default=False)  # 出口卖方是否为外贸主体(星盟)
    default_port = Column(String(64), default="宁波")
    default_origin_place = Column(String(64), default="杭州")  # 报关货源地
    # 该店铺默认要生成的文件类型清单，JSON list[str]（如 ["托书","报关资料","投保单"]）。
    # 空/null = 全部。生成区按此自动勾选对应模板（店铺doc_types ∩ 本批货代+内部的启用模板）。
    doc_types = Column(Text)
    remark = Column(Text)
    active = Column(Boolean, default=True)


class Brand(Base):
    """品牌——绑定关系的主键：品牌 → 默认工厂 + 店铺主体。"""
    __tablename__ = "brands"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="", index=True)   # Byane / Serenorch…
    abbr2 = Column(String(8), default="")               # 编号用前2位大写，可改
    factory_id = Column(Integer, ForeignKey("factories.id"))
    company_id = Column(Integer, ForeignKey("companies.id"))
    # 编号规则模板，占位符: {brand2} {fc} {date} {country} {shipdate} {sa}(经星盟时="SA-",否则"")
    doc_no_rule_shipment = Column(String(128), default="{sa}{brand2}-{fc}-{date}")
    doc_no_rule_purchase = Column(String(128), default="{sa}{brand2}-{country}-{date}")
    sellfox_supplier_id = Column(String(32), default="")   # 赛狐供应商id（生成采购单用，链条系21797/嘉欣539122）
    sellfox_warehouse_id = Column(String(32), default="")  # 赛狐仓库id（采购单，空则用采购计划默认仓库）
    default_site = Column(String(16), default="美国")       # 默认站点（未建仓采购计划补站点用）
    amazon_store = Column(String(32), default="")           # mcapi AMAZON_STORES_JSON 的 store key（空=默认店 main）
    remark = Column(Text)
    active = Column(Boolean, default=True)


class PurchasePlanConfirm(Base):
    """采购计划的人工工厂确认 + 采购单生成记录（本地工作流，与赛狐两边并行）。
    采购计划本身实时拉赛狐，本表只存确认状态/快照/生成的采购单号。"""
    __tablename__ = "purchase_plan_confirms"
    id = Column(Integer, primary_key=True)
    plan_group_no = Column(String(64), unique=True, index=True)  # PPG…
    status = Column(String(16), default="待审核")   # 待审核/待采购/待下单/待到货/已完成
    brand_name = Column(String(64), default="")
    supplier_id = Column(String(32), default="")    # 实际用的赛狐供应商id
    warehouse_id = Column(String(32), default="")
    items_snapshot = Column(Text)                   # 确认时的明细快照 JSON（sku/品名/数量/箱数/单价）
    confirmed_by = Column(String(64), default="")
    confirmed_at = Column(DateTime)
    purchase_no = Column(String(255), default="")   # 赛狐采购单号 purchaseOrderNo(可多个逗号分隔)
    po_action = Column(String(8), default="")       # 生成时用的 action 0/1/2
    po_created_at = Column(DateTime)
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class Product(Base):
    """产品报关主数据。sku = 赛狐 MSKU 关联键。"""
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(128), default="", unique=True, index=True)
    name_customs_cn = Column(String(255), default="")   # 中文报关名（报关单）
    name_customs_en = Column(String(255), default="")   # 英文报关名
    name_contract = Column(String(255), default="")     # 合同用中文品名
    name_invoice = Column(String(255), default="")      # 发票/箱单用货物名称
    name_usage = Column(String(255), default="")        # 用途功能表品名
    hs_code = Column(String(32), default="")
    declare_elements = Column(Text)                      # 申报要素
    material = Column(String(128), default="")
    material_en = Column(String(128), default="")    # 英文材质（盈和托书 H 列）
    usage = Column(String(128), default="")
    brand_name = Column(String(64), default="")
    model = Column(String(128), default="")
    box_weight_kg = Column(Float)                        # 单箱重量（报关毛重用）
    carton_l_cm = Column(Float)                          # 箱规长(cm)（离线批次明细兜底）
    carton_w_cm = Column(Float)                          # 箱规宽(cm)
    carton_h_cm = Column(Float)                          # 箱规高(cm)
    qty_per_box = Column(Integer)                        # 单箱数量(pcs)
    unit_price_default = Column(Float)                   # 申报单价默认值（兜底）
    sort_index = Column(Integer)                         # 最近一次产品导入的行序
                                                         # （=当周补仓计划/商品列表行序，
                                                         # 采购合同聚合行序按此排）
    purchase_cost_default = Column(Float)                # 采购成本默认值（兜底）
    currency = Column(String(8), default="USD")
    image_url = Column(String(512), default="")
    remark = Column(Text)


class Forwarder(Base):
    __tablename__ = "forwarders"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="")
    sellfox_name = Column(String(64), default="")  # 与赛狐 logisticProviderName 匹配
    contact = Column(String(64), default="")
    phone = Column(String(64), default="")
    remark = Column(Text)
    # 企微沟通渠道（qiweapi）：发消息按 told→外部联系人 或 room→群
    qiwe_external_userid = Column(String(128), default="")  # 货代外部联系人 id（私聊询价）
    qiwe_room_id = Column(String(128), default="")          # 群 id（群里询价）
    qiwe_guid = Column(String(64), default="")              # 指定实例 guid（空=用 QIWE_GUID 默认）
    # 询价绑定：按品牌（用户选定维度）。bind_brand_id 命中的品牌建仓后默认询这家；
    # is_default=该品牌缺绑定时的兜底货代。多家比价时取该品牌全部绑定货代。
    bind_brand_id = Column(Integer, ForeignKey("brands.id"))
    is_default = Column(Boolean, default=False)
    active = Column(Boolean, default=True)


class Template(Base):
    """文件模板：货代/内部 Excel 模板 + 映射配置（格式见 DESIGN.md §6）。"""
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), default="")
    owner_type = Column(String(16), default="forwarder")  # forwarder / internal
    forwarder_id = Column(Integer, ForeignKey("forwarders.id"))
    doc_type = Column(String(32), default="其他")  # 托书/报关资料/投保单/采购合同/9810/装箱单/其他
    stored_file = Column(String(255), default="")
    original_name = Column(String(255), default="")
    granularity = Column(String(32), default="shipment")  # shipment/batch/row_per_shipment
    mapping = Column(Text)                                # JSON
    filename_rule = Column(String(255), default="")       # 如 {brand}-托书-{fc}-{shipdate}
    requires_forwarder_no = Column(Boolean, default=False)  # 投保=True：缺货代单号则锁定
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    forwarder = relationship("Forwarder")


class RuleConfig(Base):
    """规则配置：scope='global' 或 'company:{id}'（主体级覆盖）。"""
    __tablename__ = "rule_configs"
    id = Column(Integer, primary_key=True)
    key = Column(String(64), index=True)     # net_weight_deduction / vat_factor / markup_factor / …
    scope = Column(String(32), default="global")
    value = Column(String(255), default="")
    label = Column(String(128), default="")
    default_value = Column(String(255), default="")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ---------------------------------------------------------------- 业务数据

class Batch(Base):
    """批次 = 赛狐 STA 入库计划。"""
    __tablename__ = "batches"
    id = Column(Integer, primary_key=True)
    inbound_plan_id = Column(String(64), unique=True, index=True)
    purchase_plan_no = Column(String(64), index=True)  # 来源采购计划组号 PPG…（已导入标记+采购成本来源）
    name = Column(String(128), default="")           # 如 Serenorch-US-0619
    brand_id = Column(Integer, ForeignKey("brands.id"))
    company_id = Column(Integer, ForeignKey("companies.id"))
    factory_id = Column(Integer, ForeignKey("factories.id"))
    shop_name = Column(String(64), default="")       # 赛狐店铺名 Serenorch-US
    marketplace = Column(String(16), default="")     # US
    country = Column(String(16), default="US")
    base_date = Column(String(16), default="")       # 发货基准日(周五) YYYY-MM-DD
    contract_date = Column(String(16), default="")   # 合同日期(规则算出,可改)
    status = Column(String(16), default="已同步")     # 已同步/已核对/已生成/待投保/完成
    sop_done = Column(Text)                          # SOP 流程已手动标记完成的步骤 key（JSON list）
    placement_options = Column(Text)                # 建仓分仓方案 JSON（分仓/合仓各方案的FC/箱数/重量，未确认前对比用）
    synced_at = Column(DateTime)
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

    brand = relationship("Brand")
    company = relationship("Company")
    factory = relationship("Factory")
    shipments = relationship("Shipment", back_populates="batch",
                             cascade="all, delete-orphan", order_by="Shipment.id")


class Shipment(Base):
    """货件（per 目的仓）。"""
    __tablename__ = "shipments"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    sellfox_shipment_id = Column(String(64), default="")   # sh…
    amazon_shipment_id = Column(String(32), default="", index=True)  # FBA…
    ship_sn = Column(String(64), default="")               # 发货单号 FH…
    reference_id = Column(String(32), default="")
    fc_code = Column(String(16), default="")
    address_line1 = Column(String(255), default="")
    address_line2 = Column(String(255), default="")
    city = Column(String(64), default="")
    state = Column(String(32), default="")
    postal_code = Column(String(32), default="")
    country_code = Column(String(8), default="US")
    carton_num = Column(Integer)
    total_weight = Column(Float)                            # kg（赛狐给的）
    total_volume = Column(Float)
    expect_arrival_start = Column(String(32), default="")
    expect_arrival_end = Column(String(32), default="")
    forwarder_id = Column(Integer, ForeignKey("forwarders.id"))
    forwarder_order_no = Column(String(64), default="")     # 货代单号（人工回填，投保用）
    status = Column(String(16), default="已同步")
    edited_fields = Column(Text)                            # JSON list[str]
    remark = Column(Text)

    batch = relationship("Batch", back_populates="shipments")
    forwarder = relationship("Forwarder")
    items = relationship("ShipmentItem", back_populates="shipment",
                         cascade="all, delete-orphan", order_by="ShipmentItem.id")
    boxes = relationship("ShipmentBox", back_populates="shipment",
                         cascade="all, delete-orphan", order_by="ShipmentBox.id")


class ShipmentItem(Base):
    """货件明细行（SKU 维度）。"""
    __tablename__ = "shipment_items"
    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))
    msku = Column(String(128), default="")
    fnsku = Column(String(32), default="")
    asin = Column(String(32), default="")
    qty = Column(Integer)
    box_count = Column(Integer)            # caseNum
    qty_per_box = Column(Integer)          # cartonQty
    carton_l = Column(Float)               # cm（赛狐 shippingOrder 给 cm）
    carton_w = Column(Float)
    carton_h = Column(Float)
    customs_unit_price = Column(Float)
    purchase_cost = Column(Float)
    edited_fields = Column(Text)

    shipment = relationship("Shipment", back_populates="items")
    product = relationship("Product")


class ShipmentBox(Base):
    """逐箱明细（赛狐 inboundCartonVos）。"""
    __tablename__ = "shipment_boxes"
    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    box_id = Column(String(64), default="")    # FBA…U000001
    msku = Column(String(128), default="")
    qty = Column(Integer)
    length_in = Column(Float)
    width_in = Column(Float)
    height_in = Column(Float)
    weight_lb = Column(Float)

    shipment = relationship("Shipment", back_populates="boxes")


class InboundPlan(Base):
    """建仓（亚马逊 FBA STA 入库计划）本地过程记录。

    区别于 Batch（=建成的批次/赛狐STA）：本表记录"系统主动建 STA"的多步异步过程
    与中间状态；建仓完成后回填/关联到 Batch + Shipment。
    输入来自补仓计划（SKU/数量/每箱数/外箱数/箱规），不是采购计划（采购在建仓之后）。
    """
    __tablename__ = "inbound_plans"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(16), default="manual")    # manual / replenish(补仓计划) / purchase_plan
    source_ref = Column(String(64), default="")           # 来源标识（补仓计划名/采购计划组号）
    brand_id = Column(Integer, ForeignKey("brands.id"))
    store = Column(String(32), default="")                # 实际用的 mcapi store key
    name = Column(String(128), default="")                # 如 HUHOLE-US-2026.5.29-建仓
    status = Column(String(24), default="待建仓")           # 待建仓/计划已创建/装箱已确认/已提交箱规/
                                                          # 分仓方案已生成/分仓已确认/运输已配置/建仓完成/失败
    amazon_inbound_plan_id = Column(String(64), default="", index=True)
    items_snapshot = Column(Text)        # JSON [{msku,quantity,units_per_box,boxes,l_in,w_in,h_in,weight_lb}]
    source_address = Column(Text)        # JSON 发货地址快照
    packing_option_id = Column(String(128), default="")
    placement_option_id = Column(String(128), default="")
    current_operation_id = Column(String(128), default="")  # 进行中的异步操作（前端轮询用）
    shipments_snapshot = Column(Text)    # JSON 分仓后货件 [{shipmentId,fcCode,address,boxes,...}]
    batch_id = Column(Integer, ForeignKey("batches.id"))    # 完成后关联的批次
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    brand = relationship("Brand")


# ---------------------------------------------------------------- 货代沟通（询价/比价）
# 设计见 AGENT_FORWARDER.md。建仓产出 FC/箱数/体积 → 起草询价 → 企微发多家 →
# webhook 收回复落 ForwarderMessage → Claude 提取成 InquiryQuote → 多家比价人工选。

class Inquiry(Base):
    """一次询价（一个批次发起，可面向多家货代比价）。"""
    __tablename__ = "inquiries"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    status = Column(String(16), default="待发送")  # 待发送/已发送/收集中/已选货代/已取消
    content = Column(Text)                          # 发出的询价正文（起草确认后定稿）
    structured = Column(Text)                       # JSON 结构化询价要素（FC/箱数/体积/重量/品名…）
    target_forwarder_ids = Column(Text)             # JSON list[int]，本次询的货代
    chosen_quote_id = Column(Integer)               # 拍板选中的 InquiryQuote.id
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InquiryQuote(Base):
    """某货代对某次询价的报价（Claude 从企微消息提取的结构化结果）。"""
    __tablename__ = "inquiry_quotes"
    id = Column(Integer, primary_key=True)
    inquiry_id = Column(Integer, ForeignKey("inquiries.id"), nullable=False, index=True)
    forwarder_id = Column(Integer, ForeignKey("forwarders.id"), index=True)
    raw_message = Column(Text)             # 提取所依据的原文（往返拼接）
    price = Column(Float)                   # 头程报价
    currency = Column(String(8), default="CNY")
    unit = Column(String(16), default="")  # 计价单位（/kg、/票、/方…）
    channel = Column(String(64), default="")   # 渠道（空运/海运/快递…）
    eta_days = Column(Integer)             # 时效（天）
    cutoff = Column(String(64), default="")    # 截关/截单
    valid_until = Column(String(32), default="")  # 报价有效期
    attachments = Column(Text)             # JSON 附件信息
    extract_confidence = Column(Float)     # 0~1 提取置信度
    is_chosen = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    forwarder = relationship("Forwarder")


class ForwarderMessage(Base):
    """货代沟通消息流水（沟通留痕 + 给 Claude 提取的上下文）。"""
    __tablename__ = "forwarder_messages"
    id = Column(Integer, primary_key=True)
    forwarder_id = Column(Integer, ForeignKey("forwarders.id"), index=True)
    inquiry_id = Column(Integer, ForeignKey("inquiries.id"), index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), index=True)
    direction = Column(String(4), default="in")   # in=货代来 / out=我方发
    content = Column(Text)                          # 文本内容
    media = Column(Text)                            # JSON 媒体/附件
    msg_type = Column(String(16), default="text")  # text/image/file/link…
    qiwe_msg_id = Column(String(64), default="", index=True)  # 平台消息id，幂等去重
    raw = Column(Text)                              # 原始 webhook/发送 payload（调试+解析迭代）
    ts = Column(DateTime, default=datetime.now)     # 消息时间
    created_at = Column(DateTime, default=datetime.now)


class GeneratedDoc(Base):
    __tablename__ = "generated_docs"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    shipment_id = Column(Integer, ForeignKey("shipments.id"))  # 批次级文件为空
    template_id = Column(Integer, ForeignKey("templates.id"))
    doc_type = Column(String(32), default="")
    filename = Column(String(255), default="")
    path = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.now)

    template = relationship("Template")
