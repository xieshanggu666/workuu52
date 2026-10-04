# 流域洪水预报与水库群联合调度系统

基于水文模拟引擎的流域级洪水预报与水库群削峰错峰调度演示系统：覆盖 **降雨—产流—汇流—河道演算—水库调洪—联合调度—预警与转移** 全链条。

![Tech](https://img.shields.io/badge/Backend-FastAPI%20%2B%20SQLite-009688) ![Tech](https://img.shields.io/badge/Frontend-Vue3%20%2B%20SVG-42b883) ![Tech](https://img.shields.io/badge/Python-3.10%2B-3776AB)

---

## 功能特性

- **水文模拟引擎**
  - SCS-CN 曲线数法产流（含初损扣损）
  - 三角单位线汇流（面积—时段守恒折算）
  - 马斯京根法河道演进（C0+C1+C2=1 保证稳定）
- **水库调洪演算**：库容—水位—泄流三曲线耦合的固定点迭代，支持闸门开度过程线；出流受「库容+时段入流」可用水量约束，杜绝空库凭空泄流
- **三工况对比**（削峰率以天然过流为基准）
  - `natural` 天然河道过流（无水库调蓄，最坏基线）
  - `rule` 常规调度规则（非汛情期顺水控泄，强降雨峰前预泄腾库）
  - `optimized` 联合优化调度（倍率扫描 + 错峰错时微调，保证不劣于前两者）
- **预警与转移**：站点水位分级预警（蓝/黄/橙/红）、淹没风险区评估、强制转移记录与转移路线可视化
- **预报幂等**：预报运行与处置台账建立幂等关联——重复执行同一预报直接复用既有运行（过程线 + 预警 + 转移完整回放），不重复新增台账；支持 `Idempotency-Key` 请求头、`force` 强制重跑、并发触发收敛、异常安全重试与历史库自动迁移
- **可视化前端**：SVG 流域河网拓扑图、实时监测、情景预报对比、闸门运行过程、削峰效果评估

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.10+ · FastAPI · SQLAlchemy 2 · SQLite |
| 前端 | Vue 3（本地 UMD 全量构建）· 原生 SVG 图表 · 无构建链 |
| 测试 | pytest（37 个用例：水文/水库/幂等/并发/迁移） |

## 目录结构

```
flood_system/
├── app/
│   ├── main.py               # FastAPI 入口（静态托管 + 路由）
│   ├── api/router.py         # 全部 REST 接口
│   ├── core/                 # 配置 / 数据库 / 轻量迁移（幂等列与索引）
│   ├── models/               # 数据表（流域/河网/水库/站点/情景/预警/幂等索引…）
│   └── services/
│       ├── hydrology.py      # SCS-CN / 单位线 / 马斯京根 / 拓扑排序
│       ├── reservoir.py      # 调洪演算 + 三工况联合调度
│       ├── idempotency.py    # 输入指纹 / 指纹锁 / 心跳接管
│       └── forecast.py       # 预报编排 / 幂等协调 / 台账关联 / 结果回放
├── static/                   # Vue 前端（无构建，直接托管）
│   ├── index.html
│   ├── css/style.css
│   └── js/                   # api / charts / 5 个视图组件
├── scripts/init_db.py        # 一键重建演示库（青岚江流域）
├── tests/                    # pytest 单元测试
├── data/                     # SQLite 运行库（运行时生成）
├── package.json              # npm run dev 一键启动
└── requirements.txt
```

## 快速启动

```bash
# 1. 安装后端依赖
pip install -r requirements.txt

# 2. 一键启动（自动初始化演示库 → 启动前后端 → 打开浏览器）
npm run dev
```

`npm run dev` 会自动：删除旧库 → 重建演示数据（青岚江流域）→ 在 http://127.0.0.1:8071 启动后端 → 打开浏览器。前端静态资源由后端直接托管，无需任何构建。

## 使用说明

1. **流域总览** — 青岚江流域河网拓扑、子流域、雨量站、水库与风险区分布
2. **实时监测** — 雨量/水位/水库蓄水状态一览与运行状态着色
3. **洪水预报** — 选择降雨情景（5 年/100 年/10 年一遇），推演 36 小时洪水过程；默认以天然工况展示最不利情形，可切换规则调度与联合优化对比
4. **联合调度方案** — 三工况下游峰值对比、削峰率、闸门开度与库水位过程线
5. **预警与转移** — 分级预警台账、风险区受威胁评估、强制转移路线与人口转移进度

## 运行测试

```bash
python -m pytest tests -q
```

覆盖：SCS 产流守恒、单位线归一化与单峰性、卷积线性、马斯京根稳定性、河网拓扑排序、水库水量平衡/空库约束、优化方案不劣于基线。

## 核心 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` | 流域统计总览 |
| GET | `/api/map` | 河网拓扑 / 水库 / 风险区 |
| GET | `/api/rain-events` | 降雨情景列表 |
| GET | `/api/rain-events/{id}` | 情景雨量过程 |
| POST | `/api/forecast/{event_id}/{mode}` | 执行（或幂等复用）洪水预报；支持 `Idempotency-Key` 头、`?force=true` |
| GET | `/api/forecast/runs` | 历史预报记录（含 status / input_fingerprint） |
| GET | `/api/forecast/runs/{run_id}` | 按运行 ID 回放已完成预报结果 |
| GET | `/api/forecast/series/{run_id}` | 运行原始过程线 |
| GET | `/api/warnings?scope=active\|all\|superseded` | 预警台账（默认仅生效；含历史已替代） |
| GET | `/api/evacuations?active_only=true\|false` | 转移行动记录（默认仅进行中） |

## 预报幂等设计

「重复点击/定时重跑同一预报」过去会反复新增预警与转移台账。系统在预报运行
（`forecast_runs`）与处置记录（`warning_records` / `evacuation_records`）之间
建立了如下幂等关联：

- **输入指纹**：对降雨情景、子流域/河网/水库/水位站/风险区参数与引擎版本
  （`ENGINE_VERSION`）做规范化 SHA-256。相同指纹的重复请求直接回放已完成运行，
  响应 `idempotent_replay=true`，**不再新增任何运行、过程线、预警、转移记录**。
- **台账关联与历史保留**：预警/转移记录携带 `run_id`。新预报使同目标旧预警置为
  `superseded`（记录 `superseded_by_run_id`，历史不删除，台账默认只见最新 active）；
  同风险区进行中的转移行动被复用（`linked_run_ids` 追加），不重复建单。
- **并发触发**：进程内按指纹串行锁收敛；跨进程由 `forecast_runs` 上的部分唯一索引
  （running/done 各至多一条）仲裁，后来者轮询等待首个执行者并回放其结果。
  SQLite 启用 WAL + busy_timeout。
- **异常重试**：运行状态机 `running → done / failed`，带 owner 与心跳；
  崩溃残留的 running（5 分钟无心跳）可被 CAS 接管；failed 不占唯一名额，可安全重试，
  落库在单事务内完成（清理半成品 → 替代旧台账 → 写过程线/台账 → done）。
- **显式幂等键**：请求头 `Idempotency-Key`（防前端连点/网关重试）；同键不同参数返回
  `409 Conflict`。`POST ...?force=true` 可强制重跑（旧运行置 superseded，历史保留）。
- **历史库迁移**：启动时 `app.core.migrate` 增量 `ALTER TABLE` 补列并
  `CREATE INDEX IF NOT EXISTS`，不删数据；历史 `run_id=NULL` 记录与唯一索引共存
  （SQLite 唯一索引中 NULL 互不冲突）。
