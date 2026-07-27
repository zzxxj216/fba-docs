# FBA 发货文件管理系统（fba-docs）

FBA 建仓后文件准备一体化：赛狐拉批次 → 校验 → 建仓 → 询价比价 → 一键生成（托书/报关/
投保/采购合同）→ 归档。运营端配合 Codex/Claude Code 按 `AGENTS.md` 对话驱动本地 API。

## 运营端快速开始

```bash
git clone <本仓库>
cd fba-docs
pip install -r requirements.txt
copy .env.example .env        # 填 MCAPI_BASE / MCAPI_KEY / OPERATOR_NAME（无需任何密钥）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # 严禁 --reload
```

首次初始化：向管理员索取 `seed_pkg.zip`（主体档案/规则/文档/模板，**离线分发，勿入 git**），
`curl -F "file=@seed_pkg.zip" http://127.0.0.1:8000/api/seed/import-pkg` 导入后即可使用。

- 本地库默认 SQLite（`fba_docs.db`），业务数据/生成文件全在本机。
- 外部服务（亚马逊 SP-API/赛狐/企微）一律经服务器 mcapi 网关代理，密钥不落本机。
- 关键写操作（建仓/下采购单/确认分仓/发询价）会先到服务器占坑防重复，409=他人已操作。
- **红线**：一切删除/取消/作废操作不由 AI 执行，见 AGENTS.md。

架构与改造背景见 `OPENSOURCE_PLAN.md`；模块设计见 `DESIGN.md`。
