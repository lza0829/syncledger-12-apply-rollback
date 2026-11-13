# 03-syncledger（双核对账）

Syncledger — 跨库/跨系统对齐对账：双端抽取 → 对齐键对账 → 差异处置 → 批次互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5300 |
| API | http://localhost:10300 |

## 主链

配置对账对与对齐键 → 触发双端抽取并对齐 → 落差异行与批次计数 → 页面互证

## 种子对账对

- **inventory_hq_vs_store**：总部库存 ↔ 门店库存（`sku_id`+`store_id`，比对 `qty`）
- **payment_vs_ledger**：支付网关 ↔ 账务流水（`txn_id`，比对 `amount`/`currency`）
- **sku_catalog_a_vs_b**：目录 A ↔ 目录 B（`sku_code`，比对 `name`/`category`）

底座默认只**记差异、不自动盖写**；差异处置/契约/审计页留表与入口，深逻辑留给后题。侧栏不叫「工单」，避免撞禁名单「工单客服」。

## 盖写与回滚（差异处置页）

可审计盖写与回滚，规则如下（`backend/app/services/apply_service.py`）：

- **盖写必留旧值**：盖写前把目标端当前行整体快照进 `old_values`；目标端无此行（单侧差异）一律拒绝盖写并留 `apply_blocked` 痕，绝不记盖写成功。
- **回滚标脏待重对**：回滚成功把侧数据还原为旧值，记录置 `rolled_back` 并标脏；脏标记只能被「重对」（本对账对跑完一趟新对账批次）清除，回滚失败/进行中不会清脏。
- **回滚中禁盖写**：记录进入 `rolling_back` 后，同一键禁止再次盖写（`apply_blocked` 留痕）；意外中断的键停在 `rolling_back`、批次停在 `running`，现场可查。
- **回滚失败不标成功**：恢复前校验（缺旧值 / 侧数据缺失 / 当前值已漂移）失败则置 `rollback_failed` 并标脏，留 `rollback_failed` 痕。同键多笔盖写须按 LIFO 回滚，否则拒绝并留 `rollback_rejected` 痕。
- **批量回滚策略 = 部分成功（partial_success）**：逐键独立提交——已回滚键保持回滚结果、失败键留痕、批次按结果记 `completed` / `partial` / `failed`；不做整批补偿回盖。
- **计数可核对**：盖写数、回滚成功/失败数、仍脏键数由 `/api/apply/stats` 与留痕（`apply_events`）及侧数据（`side_values`）交叉核对，页面展示一致性结论。

侧数据（`side_values`）是被盖写端的可变覆盖层；对账按「夹具 + 侧数据」重算，因此盖写后重对差异消失、回滚后差异重现。

接口：`POST /api/apply`（`diff_id`+`direction`）、`POST /api/apply/rollback`（`apply_ids`）、`GET /api/apply/records|events|stats|side-values|rollback-batches`。

测试：`cd backend && pytest tests/`（11 条，覆盖上述各规则）。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口

已实现：`apply_rollback`（盖写与回滚，见上节）。未实现（见出题规划）：`master_slave_pick` / `side_only_policy` / `window_buffer` / `diff_disposition_flow` / `batch_idempotency` / …
