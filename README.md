# 白玉蟹联合品控结算链

本项目用于管理养殖塘口、质量批次与农户结算中的稳定事实和交换边界。仓库提供基础服务、数据库初始化入口、领域说明和一份脱敏示例，便于不同参与方在一致约定下协作。

## 目录

- `contracts/` 保存外部交换字段示例。
- `docs/` 说明领域对象、时间和标识约定。
- `src/` 保存服务代码：`db.py`（存储与迁移）、`services.py`（领域规则）、`api.py`（HTTP 接口）、`app.py`（入口）。
- `tests/` 保存基础行为检查。

## 运行

执行 `make test` 检查基础行为，执行 `make migrate` 初始化本地数据目录，执行 `make run` 启动服务。默认监听 `8080` 端口，健康检查地址为 `/health`。

配置通过环境变量传入（`PORT`、`DATABASE_PATH`），敏感值和本地数据库文件不得提交到仓库。

## 接口摘要

写接口均接收 JSON 请求体，错误返回 `{"error": {"code", "message"}}`：

- 主数据：`POST /farmers`、`/ponds`、`/contracts`（含版本化等级价格与帮扶加价）、`/seedling-batches`、`/stockings`
- 过程记录：`POST /inputs`、`/guidance`、`/harvests`、`/gradings`（分级重量必须守恒）
- 质量：`POST /inspections`（不合格自动冻结相关水体与关联批次）、`POST /freeze-actions`（复检/解封/销毁分角色签署）
- 出口：`POST /export-orders`、`/allocations`（并发防超卖）、`/delivery-receipts`
- 结算：`POST /settlements`（合同版本 × 等级 × 实收重量 × 帮扶加价）、`POST /returns`（冲正，不改原账）
- 查询：`GET /trace/{ref}`（样品回溯塘口、投入、检测与流向）、`GET /settlements/{ref}`（金额构成解释）
