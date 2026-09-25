# 企业排污许可与超标处置

汇总监测和工况，判断排放超标并跟踪复测、整改、执法与复查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、复测判定和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制、整改台账存储和审计链。
- `src/service.py`：权限检查、用例编排、失效重判、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、整改台账和失败测试。

整改台账的存储（`repository.py`）、判定规则（`rules.py`）和请求入口（`http_api.py`）分开承担，`service.py` 只做用例编排。

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
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/parameters`，修改浓度或许可限值，必须提交`expected_version`
- `GET /api/rectifications`，整改台账总览，可按`?status=open|closed`过滤
- `GET /api/items/{id}/rectifications`
- `POST /api/items/{id}/rectifications`，登记责任人、复核人与到期时间
- `GET /api/items/{id}/rectifications/{rid}/retests`，复测结论含已失效旧记录
- `POST /api/items/{id}/rectifications/{rid}/retests`，提交复测读数
- `POST /api/items/{id}/rectifications/{rid}/close`，由登记的复核人关闭整改
- `POST /api/items/{id}/rectifications/{rid}/deadline`，修改整改期限
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 整改台账规则

- 进入整改（remediation）前必须已登记整改台账：责任人、复核人与到期时间；复核人不能与责任人相同；同一事件只留一项未结束整改（数据库唯一索引保证）。
- 复测结论：读数不高于许可限值且在期限内提交为合格（pass），否则不合格（fail）。
- 逾期未交复测，或最新复测读数仍高于许可限值，事件不能进入复查（inspection）；整改未关闭同样不能进复查。
- 关闭整改要求最新复测结论合格且由责任人以外的人员提交（换人复测），并只能由登记的复核人执行关闭。
- 限值、浓度或期限一变，未结束整改的复测结论即失效并按新值重判；失效旧记录保留可查。已关闭整改的结论不再重判。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
