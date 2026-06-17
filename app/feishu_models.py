"""飞书线（Track-B）专属数据模型 —— 内部运营入口。

**勿动 app/models.py**（共享只读契约）。这里的新表挂到同一个 database.Base，
启动时 create_all 自动建。

- Operator：运营 ↔ 飞书用户 ↔ 管哪些店铺/品牌 的映射（认人用）。
- FeishuSession：运营在飞书唤醒后的一次工作会话（建工作文件夹、记当前批次/询价上下文）。

设计原则（见 CONTRACT_PARALLEL.md §1/§4）：
- 飞书是内部运营界面，不直接面对货代；货代在企微（询价线 qiwe）。
- 任何"最终确认/选定/发文件"动作都要人工点卡片按钮，不自动。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from .database import Base


class Operator(Base):
    """运营人员 ↔ 飞书账号 ↔ 管辖店铺/品牌 映射。

    认人：收到飞书消息时按 feishu_open_id（或 feishu_user_id）查到本人，
    再据 scope_* 决定能看哪些待采购/待建仓。空 scope = 看全部（管理员）。
    """
    __tablename__ = "feishu_operators"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="")
    feishu_open_id = Column(String(64), default="", index=True)   # ou_xxx，发消息用
    feishu_user_id = Column(String(64), default="", index=True)   # 企业内 user_id
    feishu_union_id = Column(String(64), default="")
    # 管辖范围：JSON list。空/null = 全部（管理员）。
    scope_brand_ids = Column(Text)        # JSON list[int]，管的品牌 id
    scope_shop_names = Column(Text)       # JSON list[str]，管的赛狐店铺名（兜底/补充匹配）
    is_admin = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class FeishuSession(Base):
    """一次飞书运营会话：唤醒 → 认人 → 建工作文件夹 → 体检 → 派发动作。

    chat_id 维度保活（同一会话连续操作复用），current_* 记上下文方便按钮回调时取。
    """
    __tablename__ = "feishu_sessions"
    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, index=True)
    chat_id = Column(String(64), default="", index=True)   # 飞书会话 id
    open_id = Column(String(64), default="")               # 发起人 open_id
    status = Column(String(16), default="active")          # active/closed
    work_dir = Column(String(512), default="")             # 本次会话工作文件夹
    current_batch_id = Column(Integer)                     # 当前操作的批次
    current_inquiry_id = Column(Integer)                   # 当前询价（接缝：询价线产出）
    last_intent = Column(String(32), default="")           # 最近一次解析出的意图
    context = Column(Text)                                 # JSON 自由上下文
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
