# 流域归属与观测服务

站点观测按**采样时的流域归属**供数。站点转属、历史更正、历史补录在一笔事务内完成，
提交时统一校验完整性；API 与任意直连 SQL 会话（READ COMMITTED）受同一套数据库约束。

## 架构

```
┌────────────┐   HTTP    ┌─────────────────┐   SQL    ┌──────────────────┐
│ 客户端/curl │ ────────▶ │ FastAPI (uvicorn) │ ──────▶ │ PostgreSQL 16     │
│ 直连 psql   │ ──────────┼──────────────────┼────────▶ │ 约束在库内强制执行 │
└────────────┘           └─────────────────┘          └──────────────────┘
```

- `db/init.sql` —— 全部约束逻辑（表、延迟约束触发器、站点行锁、供数视图、站日 ETag 版本表与触发器），数据库启动时装载，持久化于系统目录
- `api/` —— FastAPI 应用（变更集 + 转属/拆分/合并/更正便捷端点 + 供数查询 + 站日 ETag/304）
- `docker-compose.yml` —— `db`(postgres:16-alpine) + `api`，健康检查串联启动顺序
- `examples/` —— 转属、历史更正、直连 SQL、并发冲突、ETag 条件请求 五个示例
- `tests/` —— pytest 集成测试（28 个用例）

## 数据模型与约束

- `stations(station_id, name)` —— 站点，同时充当**每站一把的锁锚点**
- `attribution_periods(period_id, station_id, watershed, valid_from, valid_to)`
  —— 归属时段，`[valid_from, valid_to)` **左闭右开**，`valid_to` 可空（至今有效），`CHECK (valid_to > valid_from)`
- `observations(observation_id, station_id, observed_at, value)` —— 观测
- `observation_attribution`（视图）—— 供数：观测按 `observed_at ∈ [valid_from, valid_to)` 关联流域
- `observation_day_versions(station_id, day, version)` —— 站日版本表（ETag 支撑）：
  AFTER 行触发器在每次已提交写入后把受影响站日版本 +1，行持久化于库内，重启后仍可比对

两条完整性规则由 **`DEFERRABLE INITIALLY DEFERRED` 约束触发器在提交时**统一校验：

1. 同一站点任意两个时段不得重叠（`tstzrange … && …`，GiST 索引加速）；
2. 每条观测在采样时刻必须落入本站某一个时段（不重叠 ⇒ 至多一段；覆盖检查 ⇒ 至少一段；合起来**恰好一段**）。

事务**中途允许暂时违规**（先拆/删时段再补、先补观测再改时段均可），**提交时必须全部满足**，
否则整笔回滚。任何写路径（API、变更集、直连 SQL）都走同一套约束。

### 并发设计（撤段 vs 补录）

- 行级 BEFORE 触发器在每次写时段/观测前对该站点行执行 `SELECT … FOR UPDATE`
  （多站按字典序加锁，降低死锁概率）——**同站并发写被串行化**；
- 在 READ COMMITTED 下，锁等待解除后提交时校验能看到对方**已提交**的最新状态：
  - 撤段先提交 ⇒ 补录提交时发现观测无归属 ⇒ **补录整笔拒绝**；
  - 补录先提交 ⇒ 撤段提交时发现会留下孤儿观测 ⇒ **撤段整笔拒绝**；
  - 两种时序都不会出现"各自通过却留下无归属观测"；
- 不同站点持不同锁行，**无关站互不阻塞、可各自提交**；
- 死锁（多站交叉加锁）由 PG 检测，API 映射为 409，可整笔重试；
- 禁止 `TRUNCATE`（会绕过行触发器）；约束保存在系统目录，**重启后仍有效**。

## 快速开始

```bash
docker compose up -d --build --wait
curl http://localhost:8000/health        # {"status":"ok"}
xdg-open http://localhost:8000/docs      # Swagger UI（或浏览器打开）
```

数据库同时暴露在 `localhost:5432`（用户/密码/库名均为 `watershed`），便于直连 SQL。

## API 一览

| 方法/路径 | 说明 |
|---|---|
| `POST /stations` · `GET /stations` | 建站 / 列表 |
| `GET /stations/{sid}/periods` | 查某站归属时段 |
| `POST /changesets` | **核心写入口**：一笔事务按序执行多个 `insert/update/delete_period`、`insert/update/delete_observation` 操作，提交时统一校验，失败整笔回滚（409） |
| `POST /observations` | 单条补录（等价于单操作变更集） |
| `POST /stations/{sid}/transfer` | 转属：`at` 时刻截断当前时段并开启新流域时段 |
| `POST /periods/{pid}/split` | 在 `at` 处拆分为两段（右段可换流域） |
| `POST /stations/{sid}/merge` | 合并 `at` 处首尾相接的两段 |
| `POST /stations/{sid}/correct` | 历史更正：把子区间改属另一流域（自动切分原时段） |
| `GET /stations/{sid}/observations` · `GET /attributed-observations` | 供数：按采样时刻关联流域（只见已提交状态） |

### 站日 ETag（条件请求）

`GET /stations/{sid}/observations` 在 `from`/`to` **恰好圈定一个 UTC 日**
（`from = 某日 00:00Z`、`to = 次日 00:00Z`，支持等价的带偏移量表示）时提供 ETag：

- 响应带 `ETag: "obs-vN"`（强验证器，不透明标签）与 `Cache-Control: no-cache`；
- 客户端回传 `If-None-Match`，标签与当前站日版本一致时直接返回 **304**（无响应体、
  不跑明细查询），否则返回 **200 + 当前标签 + 明细**；支持 `W/` 弱前缀、多标签列表与 `*`；
- 标签来自库内持久化表 `observation_day_versions` 的单调版本号：
  - 观测增删改 → 观测的**新旧站日**都换标签（改站/改采样时刻同时触碰两边；删空当天同样换）；
  - 归属时段增删改 → 只刷新**新旧范围覆盖到观测**的站日（无观测的日子不产生版本变化）；
  - 触发器对 API 与直连 SQL 一视同仁；回滚事务对版本的修改一并回滚，**回滚后原标签仍有效**；
  - 版本行持久化在磁盘上，**重启后旧标签可继续使用**；
- 整日查询先对站点行取 `FOR SHARE` 锁（与写事务的 `FOR UPDATE` 互斥），保证返回的
  标签与明细对应**同一个已提交状态**；
- 非整日查询（缺参数、跨度不为一天、边界不在 UTC 午夜）不发 ETag，行为与历史一致；
  空日也有稳定标签（首次整日查询时懒创建版本行）。

`update_period` 中 `valid_to` 缺省表示不修改；显式置空（恢复"至今有效"）用 `"clear_valid_to": true`。

## 示例

（示例脚本依赖 `curl` 与 `jq`；SQL 示例也可用本地 `psql "$DATABASE_URL" -f examples/03_direct_sql.sql` 运行）

```bash
bash examples/01_transfer.sh                # 转属：历史观测归属不变, 新观测属新流域
bash examples/02_historical_correction.sh   # 历史更正：子区间改属 + 同事务补录
docker compose exec -T db psql postgresql://watershed:watershed@localhost:5432/watershed \
  -f - < examples/03_direct_sql.sql          # 直连 SQL：中途违规→提交前修复→成功; 提交仍违规→整笔回滚
bash examples/04_concurrent_conflict.sh      # 并发：撤段 vs 补录, 必有一方整笔被拒
bash examples/05_etag.sh                     # ETag：整日查询 304 复用、更正后重取 200 新标签、删空当天
```

`04` 的场景 1 依赖正常的锁等待/唤醒语义，请在 Docker/常规 Linux 环境运行
（个别阻断进程间信号的沙箱中，锁等待方无法被唤醒）。

## 测试

```bash
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest tests -v          # 需要 compose 栈已启动
# 可选: API_URL / DATABASE_URL 指向其他实例; PG_LOCK_WAIT_OK=1 启用真并发锁等待用例
bash scripts/selftest.sh                     # 一键: 起栈 + 全部示例 + pytest
bash scripts/restart_check.sh                # 重启 db/api 后约束仍生效(409/SQL ERROR)
bash scripts/etag_restart_check.sh           # 重启后旧 ETag 仍 304, 新写入仍换标签
```

## 验证状态（如实说明）

已在 **PostgreSQL 16.16 + FastAPI（本机直连，与 compose 相同的 init.sql 与启动命令）** 上验证：

- ✅ 28 个集成用例中 27 通过、1 个按环境跳过：原有 15 个约束/供数用例（14 通过 1 跳过）
  全部保持，新增 13 个 ETag 用例全部通过——整日发标签与 304、弱前缀/多标签/`*`、
  空日稳定标签、观测增删改刷新新旧站日、删空当天换标签、跨站移动双刷新、
  时段变更只刷有观测的站日、转属/历史更正换标签、直连 SQL 同触发、回滚保标签、
  标签与明细同快照、UTC 偏移归一；
- ✅ 示例 01/02/03/05 实际运行输出正确；`examples/05_etag.sh` 完整演示
  「200 取标签 → 304 复用 → 更正后旧标签 200 + 新标签 → 再 304 → 删空当天换标签且仍可 304」；
- ✅ 重启持久性：在数据目录的**独立副本**上用全新 postmaster 进程打开（等效重启，
  含 WAL 崩溃重放），版本行/触发器完好，旧标签 `"obs-v2"` 经全新 API 进程条件请求仍 304，
  重启后写入正确推进到 v3；约束与 ETag 触发器均为持久化目录对象；
- ✅ `/docs` 与 `/openapi.json` 可访问（HTTP 200），304 响应与 `If-None-Match` 参数已入 OpenAPI。

未能在本环境验证（本机无 Docker，且沙箱阻断进程间信号导致**锁等待/进程重启无法执行**）：

- ⚠️ `docker compose up` 的端到端启动未实际执行；compose/Dockerfile 本次未改动，
  栈内各组件（同一 `init.sql` 的 PG16、同一 uvicorn 命令）已分别本机验证；
- ⚠️ 真并发下"锁等待→唤醒→提交时被拒"的跨进程路径（`PG_LOCK_WAIT_OK=1` 的用例、
  示例 04 场景 1）在本沙箱跳过；其**拒绝逻辑**已由顺序化等价用例覆盖
  （后者看到前者已提交状态后，提交时校验拒绝），锁等待/唤醒本身为 PostgreSQL 既有机制；
- ⚠️ `scripts/restart_check.sh`、`scripts/etag_restart_check.sh` 的真实进程重启
  （`docker compose restart` / 信号）未在本沙箱执行，已用数据目录副本 + 全新进程等效验证。

## 设计取舍与限制

- 约束用延迟触发器而非排他约束：覆盖检查需要"观测必须有归属"这类跨表规则，
  且要求事务中途可违规、提交时统一校验；
- 提交时校验按站点去重（会话临时表 `pending_station_checks`，`ON COMMIT DELETE ROWS`），
  一笔事务改 N 行只对该站检一次；站点行再次被修改时"已检"标记失效（兼容 `SET CONSTRAINTS IMMEDIATE`）；
- 超级用户/表主仍可 `DISABLE TRIGGER` 或 `session_replication_role=replica` 绕过——
  数据库层面无法防止拥有者自毁，生产上应收回应用账号的 DDL 权限；
- 校验为每站 `O(时段数² → 索引下近似线性)` + 一次反连接扫描，适合站点级并发负载；
  超大批量导入建议走 `/changesets` 单事务提交。
- ETag 用库内版本号而非内容哈希：增量写入只做一次 upsert，避免每次写入重算整日哈希；
  版本号语义不透明（客户端只做原样比对），`FOR SHARE`/`FOR UPDATE` 锁对保证标签与明细
  取自同一已提交快照；代价是写放大极轻（每个受影响站日一行），且版本号只增不减
  （回滚事务的 bump 随之回滚，不会留下空洞以外的跳号问题）。
