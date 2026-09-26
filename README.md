# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额、恢复保费、恢复次数闸门和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（含恢复台账与逐次消耗明细）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：记录与恢复台账演示页面。
- `tests/`：完整流程、规则计算、恢复台账和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数；每条记录内嵌`reinstatement`恢复台账快照。
- `GET /api/records/{id}`：记录详情（含恢复台账快照、审计时间线入口）。
- `GET /api/records/{id}/audit`：审计时间线，结算事件含本次恢复次数消耗明细。
- `GET /api/reinstatements`：按事件列出全部恢复台账（登记次数、已用/剩余、累计摊回与保费）。
- `GET /api/reinstatements/{event_id}`：单事件台账详情，含`entries`逐次结算消耗明细。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 恢复次数台账规则

- 建案时必填非负整数`reinstatement_count`，按事件（`event_id`）登记可用恢复次数；同一事件重复申报的次数必须与首次登记一致，否则返回`409 conflict`。
- 仅`settle`（结算）会消耗一次恢复，并按本案摊回金额 × `reinstatement_pct` 累计恢复保费；消耗与记录状态更新在同一事务内完成。
- 未结算或`rejected`（拒赔）案件不占用次数。
- `submit_claim`（受理）与`calculate`（核定）前校验剩余次数；次数耗尽时返回`409 reinstatement_exhausted`，错误体`details`写明`used_count`/`total_count`/`short_count`（缺少次数）/`accumulated_premium`（累计恢复保费）。
- 台账与消耗明细持久化在`reinstatement_ledger`、`reinstatement_entries`表，服务重开后仍可逐笔核对。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、恢复台账（登记/消耗/累计保费/拒赔不占次数/重启持久化/次数不一致冲突）、重复引用、权限拒绝和版本冲突。
