# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额、恢复次数台账和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/reinstatements`：恢复台账列表，按事件展示可用/已用次数与累计恢复保费。
- `GET /api/reinstatements/{event_id}`：单个事件的台账与逐笔消耗明细。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`需包含`event_id`、`attachment`、`limit`、`cession_pct`、`loss_amount`、`reinstatement_pct`、`aggregate_prior`和`reinstatement_count`（该事件的可用恢复次数）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 恢复台账

台风季同一巨灾事件会多次报案，恢复责任次数按事件登记在台账中：

- 创建记录时按`event_id`登记`reinstatement_count`；同一事件再次登记的次数必须一致，否则创建被拒。
- `settle`结算时在同一事务内消耗一次恢复次数，并按摊回金额×恢复比例累计恢复保费，同时写入逐笔消耗明细。
- 次数耗尽后`calculate`核定被拒，错误中写明已用次数、缺少次数和累计保费。
- `submit_claim`受理时把已用次数、剩余次数和累计保费快照写入记录。
- 未结算或拒赔的案件不占次数。
- 记录详情（`GET /api/records/{id}`）附带当前台账快照；台账持久化在SQLite中，服务重启后仍可核对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
