# CLAUDE.md

FBA 发货文件管理系统：替代影刀 RPA，把 FBA 建仓后文件准备（托书/报关/投保/采购合同/9810）统一为"赛狐拉取批次 → 校验 → 一键生成 → 归档"。**完整设计见 `DESIGN.md`（数据模型、规则、流程、待确认问题），改功能前先读它。**

## 运行

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000   # 或双击 start.bat
# 前端: http://127.0.0.1:8000  (static/ 下 Vue3 CDN 单页，无构建步骤)
```

## 数据库

MySQL（本地）：`mysql+pymysql://root:123456@127.0.0.1:3306/fba_docs?charset=utf8mb4`
- 库名 `fba_docs`，启动时自动建库建表（`app/database.py`）
- 连接参数在 `.env`（`DB_URL` 可覆盖默认值）

## 架构（四层，路由不直接做业务）

```
app/routers/*.py → app/services/*.py → app/{sellfox_client,excel_engine,rule_engine}.py → 外部
static/           前端（Vue3 CDN，index.html + app.js + style.css）
templates_store/  上传的货代模板原件
output/{批次}/    生成文件归档
```

## 关键约定（违反会出业务事故）

0. **Claude 没有任何删除/取消权限（绝对红线）**：亚马逊入库计划/货件的取消、赛狐单据的作废/驳回、本地批次/数据的删除——**一律由用户手动执行**。Claude 不得调用任何 cancel/void/delete 类接口或删数据，即使是"测试残留""重建前清理"也不行；需要清理时**只报告、等用户处理**。（背景：曾有批量取消真实在途货件的事故。）
1. **字段字典是唯一取值入口**：模板映射只能引用 `app/field_registry.py` 里登记的路径（`batch.* / shipment.* / item.* / box.* / company.* / factory.* / trade.* / calc.* / meta.*`）。新增字段先登记再使用。
2. **Excel 写入必须显式指定 sheet**——影刀就是因为空 sheet 名把箱数写进地址库（见 DESIGN.md）。
3. **规则不许硬编码**：日期(下周五/上月同日)、净重(−0.5kg/箱)、×1.13、×1.1、保价档位等全部走 `rule_engine` + RuleConfig 表。
4. **人工修改优先**：Shipment/Item 的 `edited_fields` 里记录的字段，重新同步赛狐时不覆盖。
5. **校验通过是生成的强制前置**（用户拍板），生成失败按模板逐个报错不互相影响。
6. 赛狐 API：1次/秒限流，签名逻辑在 `app/sellfox_client.py`，凭据在 `.env`（勿提交）。

## 业务背景速查

- 层级：批次(赛狐STA入库计划) → 货件(per目的仓FC) → 明细行(SKU) → 逐箱
- 品牌是绑定主键：品牌 → 默认工厂 + 店铺主体；外贸主体=杭州星盟(SA)
- 编号：货件级 `[SA-]{brand2}-{fc}-{date}`、采购合同 `[SA-]{brand2}-{country}-{date}`，SA前缀=经星盟二级链路
- 采购合同两级：工厂→店铺(×1.13) 和 店铺→星盟(×1.13×1.1)
- 历史案例（验收基准）：`C:\Users\zane\Downloads\Serenorch-US-6.5\`（源数据+模板+RPA成品）
- 旧 RPA 源码（规则出处）：`C:\Users\zane\AppData\Local\ShadowBot\users\831334371139649536\apps\`（1c268d8a=跨运通, ffb2c539=盈和）
- 工厂/主体初始化数据：`F:\聊天记录\店铺主体-工厂信息库.xlsx`

## AI 映射助手

`app/ai_mapping.py`，Anthropic SDK，模型 `claude-opus-4-8`，structured outputs（`output_config.format` json_schema）。`ANTHROPIC_API_KEY` 未配置时优雅降级（返回提示，不崩）。

## 验收基准（P2）

用 Serenorch-US-6.5 历史批次在本系统生成全套文件，与 RPA 成品逐单元格 diff，差异需逐项定性（RPA bug / 本系统 bug）。
