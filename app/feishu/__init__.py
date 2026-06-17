"""飞书线（Track-B）包：内部运营入口的飞书机器人 agent。

- cards.py     交互卡片构造（待办卡片、动作结果卡片）
- intake.py    初始化流程（认人 → 建会话 → 拉待采购 → 建仓就绪体检 → 待办卡片）
- service.py   编排（意图理解 → intake / 按钮动作分发）
- handlers.py  长连接事件分发（运营消息 / 卡片按钮回调）
- inquiry_port.py  与询价线（Track-A inquiry_service）的接缝适配层（未就绪时 stub）
"""
