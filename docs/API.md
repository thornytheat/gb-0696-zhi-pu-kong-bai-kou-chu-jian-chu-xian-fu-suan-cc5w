# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`GET /api/export/{id}`：唯一证据CSV，按 `kind` 分 manifest/result/amendment/blank/quarantine。result 每样一行、只含最终检出结论；amendment 保留切换前后两次空白、浓度、结论与限定符。`GET /api/runs/{id}` 返回 `amendments`，同一序列重跑幂等。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
