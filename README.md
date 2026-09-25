# 企业排污许可与超标处置

汇总监测和工况，判断排放超标并跟踪复测、整改、执法与复查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口为`8313`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`；目标为`remediation`时必须同时提交`remediation: {assignee, reviewer, due_at}`
- `POST /api/items/{id}/parameters`，按`expected_version`修改浓度`quantity`或许可限值`threshold`
- `GET /api/items/{id}/remediations`、`GET /api/items/{id}/remediations/{rid}`：整改台账（含全部复测与重判记录）
- `POST /api/items/{id}/remediations/{rid}/retest`，提交复测读数`value`
- `POST /api/items/{id}/remediations/{rid}/amend`，调整整改期限`due_at`
- `POST /api/items/{id}/remediations/{rid}/close`，复核人关闭整改
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 整改台账规则

- 进入整改前必须登记责任人、复核人与到期时间，登记与状态转换同事务提交；同一事件只留一项未结束整改（数据库唯一索引保证）。
- 逾期未交复测，或最新有效复测读数仍高于许可限值，事件不能进入复查；复测合格但未由复核人关闭同样不能进入复查。
- 复测须换人进行（责任人不能复测本人整改）；复测合格后由登记的复核人关闭整改。
- 限值、浓度或期限任一变化，未结束整改的现有结论即失效（标记`invalidated`），并按新值重判生成`rejudgment`记录；被取代的复测标记`superseded`，旧记录均保留可查。
- 台账存储在`src/repository.py`，判定规则在`src/rules.py`，请求入口在`src/http_api.py`，用例编排在`src/service.py`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
