# 星载推进剂隔离装置 · 联锁捕获冻结复核

切换推进剂隔离装置前，审查员对**带时钟抖动的联锁捕获**做形式化复核：以精确有理数
区域（DBM, Difference Bounded Matrix）传播全部可能时刻，判定是否存在某条真实轨迹越过
阀门冷却边界或确认超时边界。只有**所有可能轨迹在每个事件上恰有一条迁移、且最终全部抵达
终态**时，才允许“冻结通过”。

仅依赖 Python 3.11 标准库（`fractions` + `http.server`），无需第三方包。

## 复核规则

- 模型：稳定审计标识、至多 **8** 个位置、至多 **4** 个时钟、至多 **16** 条迁移（带
  **闭区间**守卫与复位集合）、终态集合；
- 事件：按捕获顺序到达、至多 **32** 项，每项给出相对时刻**闭区间** `[min, max]`；
- 每个事件：先按闭区间**推进时间**，再以候选迁移的闭区间守卫**切分**区域，命中后执行
  **时钟复位**；
- 同一 `(位置, 事件)` 的多条迁移守卫闭区间**不得重叠**（闭端点相接即判非法）；
- 所有数值为**精确有理数**（整数或如 `9/2` 的分数字符串），禁止浮点；
- 判定：
  - 存在未覆盖时刻 → `rejected / uncovered`；
  - 该位置该事件无迁移 → `rejected / no_transition`；
  - 终态检查失败 → `rejected / non_accepting`；
  - 模型非法（越限、未知引用、守卫重叠、坏有理数…）→ `rejected / illegal_model`；
- 失败结论**稳定指出最早事件**，并给出一组可直接代入的时钟取值、对应相对时延、以及
  每个阻断守卫及其逐项违背说明；成功同样给出一条可代入核对的完整轨迹；
- **重传语义**：同 `audit_id` 语义等价重传（分数表面形式、迁移/守卫/复位顺序不同不影响
  语义）回放原结论；内容不同则保留原证据并报告 `content_conflict`。

## 目录

```
app/dbm.py        精确有理数 DBM：闭包、推进、复位、守卫切分、见证点
app/model.py      模型校验（限额、闭区间、守卫两两不重叠）
app/verifier.py   逐事件区域传播、确定性覆盖判定、证据与轨迹回溯
app/store.py      语义指纹与稳定重传/冲突存储（JSON 原子落盘）
app/server.py     真实 HTTP API + /health
app/static/       复核页面（录入、提交、结论与逐事件区域证据）
tests/test_rules.py   规则测试（unittest）
tests/http_smoke.py   真实 API 冒烟
scripts/verify.sh     verify 容器入口：规则测试 + HTTP 冒烟，退出码报告结果
```

## 本地运行

```bash
python3 -m app.server                       # 默认 0.0.0.0:8000，数据落 data/reviews.json
PORT=8011 HOST=127.0.0.1 python3 -m app.server

python3 -m unittest tests.test_rules -v     # 仅规则测试
SMOKE_BASE=http://127.0.0.1:8011 python3 tests/http_smoke.py
```

打开 `http://localhost:8000/` 使用页面：内置「合法示例」「冷却缺口示例」以及两种重传
按钮。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康响应 `{"status":"ok"}` |
| `POST` | `/api/reviews` | 提交模型 + 事件序列；`201` 首次、`200` 重传/冲突、`422` 非法模型 |
| `GET` | `/api/reviews/<audit_id>` | 取回稳定存档结论 |

请求体示例：

```json
{
  "audit_id": "AUDIT-ISO-0001",
  "locations": ["INIT", "COOLING", "ARMED", "FROZEN"],
  "initial": "INIT",
  "accepting": ["FROZEN"],
  "clocks": ["tCool", "tArm"],
  "events": ["close_cmd", "confirm", "freeze_cmd"],
  "transitions": [
    {"source": "INIT", "target": "COOLING", "event": "close_cmd",
     "guard": [], "resets": ["tCool"]},
    {"source": "COOLING", "target": "ARMED", "event": "confirm",
     "guard": [{"clock": "tCool", "min": "10", "max": "20"}],
     "resets": ["tArm"]},
    {"source": "ARMED", "target": "FROZEN", "event": "freeze_cmd",
     "guard": [{"clock": "tArm", "min": "5", "max": "15"}], "resets": []}
  ],
  "event_sequence": [
    {"event": "close_cmd", "min": "0", "max": "0"},
    {"event": "confirm", "min": "10", "max": "20"},
    {"event": "freeze_cmd", "min": "5", "max": "15"}
  ]
}
```

## Docker Compose

```bash
HOST_PORT=8080 docker compose up -d web        # 可配置宿主机端口（默认 8000）
HOST_BIND=127.0.0.1 HOST_PORT=8080 docker compose up -d web
docker compose up --build verify               # 规则测试 + HTTP 冒烟后退出
docker compose ps -a                           # verify 退出状态码即结果（0 通过）
```

- `web`：对外服务，带容器健康检查（轮询 `/health`）；审计证据持久化在命名卷
  `interlock-review-data`；
- `verify`：等待 `web` 健康后执行**合法重叠时窗、冷却区间缺口、语义等价重传/内容冲突**
  的规则测试与真实 HTTP 冒烟，**全部通过即以 0 退出，任一失败以非零状态码报告**
  （`restart: "no"`，不重试）。

## 冷却缺口场景说明

当冷却守卫为闭区间 `[10,20]` 与 `[22,30]`，而确认事件抖动区间为 `[19,22]` 时，真实时刻
`tCool ∈ (20,22)` 不被任何守卫覆盖。复核器稳定报告最早事件 `confirm (#1)`，给出见证取值
（如 `tCool=21`）以及两个阻断守卫各自的违背项（高于 20 / 低于 22）。
