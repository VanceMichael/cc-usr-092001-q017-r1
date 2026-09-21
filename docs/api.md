# HTTP API

所有业务接口前缀 `/v1`，请求与响应均为 `application/json; charset=utf-8`。
写接口在单个 `BEGIN IMMEDIATE` 事务内完成；业务错误返回
`400/403/404/409/422`，体为 `{"error": ..., "type": ...}`，成功为
`{"data": ...}`。健康检查 `GET /health`。

## 主数据

| 方法与路径 | 说明 |
|---|---|
| POST `/v1/persons` | 人员与角色 |
| POST `/v1/bases` / `/v1/farmers` / `/v1/water-bodies` / `/v1/ponds` | 基地、农户、水体、塘口 |
| POST `/v1/seed-lots` / `/v1/stockings` | 苗种批次与放养 |
| POST `/v1/materials` / `/v1/applications` | 投入品与施用（禁用兽药被拒） |
| POST `/v1/guidance` | 技术指导（technician） |
| POST `/v1/certifications` | 有机认证 |

## 品控

| 方法与路径 | 说明 |
|---|---|
| POST `/v1/inspections` | 五重检测 SEED/WATER/INPUT/PRODUCT/EXPORT |
| POST `/v1/freezes` | FAIL 后由 supervisor 冻结，体含 `water_body_ids`、`batch_ids`，范围自动推导 |
| POST `/v1/freezes/{id}/retest` | inspector 复检，附复检检测单，可多次 |
| POST `/v1/freezes/{id}/release` | reviewer 凭最近 PASS 复检解封 |
| POST `/v1/freezes/{id}/destroy` | reviewer 销毁，释放未装运预留 |
| GET `/v1/freezes/{id}` | 冻结范围与签署链 |

## 生产

| 方法与路径 | 说明 |
|---|---|
| POST `/v1/harvests` | 起捕（需有效有机认证、水体未冻结） |
| POST `/v1/grades` | 分级为子批，明细 `lines:[{grade,qty_kg}]`，重量守恒 |
| POST `/v1/splits` | 装运前再拆分 |
| POST `/v1/market-samples` | 市场抽检样品挂批次 |

## 出口贸易

| 方法与路径 | 说明 |
|---|---|
| POST `/v1/export-orders` | 柜量、每柜公斤数、等级与 `conditions_json` |
| GET `/v1/export-orders/{id}/availability` | 目标/预留/已装/退货/可用库存联动 |
| POST `/v1/allocations` | 多批次分配入柜，条件更新防超卖 |
| POST `/v1/allocations/release` | 释放未装运预留 |
| POST `/v1/shipments` | 发运（预留转实发） |
| POST `/v1/receipts` | 外商交付回执 |
| POST `/v1/returns` | 退货入库，生成 RETURN 批 |

质量条件示例：`{"require_inspection_stage":"PRODUCT","require_organic":true}`。

## 合同与结算

| 方法与路径 | 说明 |
|---|---|
| POST `/v1/contracts` | 合同版本（版本自动递增） |
| POST `/v1/settlements/generate` | 财务按当前合同版本生成，可带 `harvest_ids` 与 `period` |
| POST `/v1/settlements/{id}/confirm` | 确认并入台账 |
| POST `/v1/settlements/{id}/reverse` | 冲正（整笔或 `lines` 部分；可挂 `return_id`） |
| GET `/v1/settlements/{id}/explain` | 逐行公式、冲正、净额 |
| GET `/v1/farmers/{id}/balance` | 农户台账净额 |

## 追溯与核对

| 方法与路径 | 说明 |
|---|---|
| GET `/v1/samples/{id}/trace` | 市场样品 → 塘口/投入/检测/流向 |
| GET `/v1/batches/{id}/trace` | 任一批次的同样视图 |
| GET `/v1/consistency` | 全链守恒核对，返回 `ok` 与问题清单 |
| POST `/v1/events` | 外部交换事件，强制来源序号接续、保留原始发生时间 |

## 最小流程示例

```bash
curl -s localhost:8080/v1/harvests -d '{"harvest_id":"H-1",...}'
curl -s localhost:8080/v1/grades   -d '{"grade_id":"G-1","harvest_id":"H-1",
  "lines":[{"grade":"A","qty_kg":600},{"grade":"B","qty_kg":400}]}'
curl -s localhost:8080/v1/samples/MS-1/trace
curl -s localhost:8080/v1/consistency
```
