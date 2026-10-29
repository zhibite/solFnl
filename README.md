# gmgndaily — GMGN Solana 钱包日盈利监控

定时抓取 [GMGN](https://gmgn.ai) 的 `daily_profits` 接口（`POST /pf/api/v1/wallets/sol/daily_profits`），
把每个 sol 地址的**每日买卖、转账、胜负单、已实现盈亏**存进 SQLite，前端可看、可导入地址，
对外提供 HTTP API。

> 数据源：当前**只启用 GMGN**（Debot 源暂时停用）。服务器需要能访问 `gmgn.ai`。
> 接口无需登录，但必须用 POST，且 chain 必须是 `sol`。

## 在线访问

**生产部署**：https://fnl.bquant.com （通过 Coolify）

## 快速启动（docker）

```bash
cp .env.docker .env         # 必填 GMGN_PASSWORD / SESSION_SECRET
docker compose up -d --build
# 打开 http://服务器IP:8038 ，用户名 zengsw + 上面设置的密码登录
```

> `docker compose` 会读取同目录 `.env` 做变量替换；`GMGN_PASSWORD` 为空时
> compose 会直接报错，避免用默认密码启动。密码可用 `openssl rand -base64 18`
> 生成，`SESSION_SECRET` 用 `openssl rand -hex 32` 生成。

## Coolify 部署

Coolify 上把变量配在应用环境变量里即可；数据库卷持久化说明见
[docs/DATABASE_PERSISTENCE.md](./docs/DATABASE_PERSISTENCE.md)。

## 本地开发

```bash
cd app
pip install -r requirements.txt
set DATABASE_PATH=./data.db & set GMGN_PASSWORD=你的控制台密码
python app.py               # http://127.0.0.1:8038

# 可选：回到仓库根目录跑一次全链路自检（临时库，不碰生产数据）
cd ..
pip install -r requirements-dev.txt
python app/verify_flow.py
```

## 用法

1. 登录控制台（`/`、`/wallets`、`/wallet/<地址>`、`/config` 需登录，用户名 `zengsw`；
   `/profit/<地址>` 是公开分享页，不需要登录）。
2. `/wallets` 页粘贴 JSON 或上传文件导入地址：
   `[{"address":"...","label":"聪明钱01"}]`，点“立即同步”。
3. 后台队列会自动同步当月 + 往前 N 个月：
   - 当月超过 `SYNC_INTERVAL_MIN` 分钟就会重新入队
   - 历史月份超过 `HISTORY_SYNC_INTERVAL_MIN` 分钟才会重新入队
   - 每 `SYNC_TICK_SECONDS` 秒处理一批，最多 `SYNC_BATCH_SIZE` 个钱包、
     `SYNC_MAX_REQUESTS_PER_TICK` 个 GMGN 请求；只拉真正到期的月份
   - 公开元数据（被追踪/被备注/7D 统计）由独立队列刷新，不占盈利同步批次；
     超过 `META_SYNC_INTERVAL_MIN` 的钱包会重新入队，每 `META_TICK_SECONDS`
     秒处理 `META_BATCH_SIZE` 个

> 说明：1.5 万地址、默认 `META_BATCH_SIZE=20` / `META_TICK_SECONDS=30` 时，
> meta 完整轮询约 6～7 小时；**正在查看的单地址/详情页会实时拉取**（60 秒缓存），
> 列表接口使用后台队列的缓存结果。

> 说明：`SYNC_INTERVAL_MIN` 是当月数据的“最短”间隔，实际刷新速度还受
> `SYNC_MAX_REQUESTS_PER_TICK` 限制。当前 1.5 万地址规模下，一轮当月同步仍需
> 数小时；需要更快可调大请求预算，但要注意 GMGN 限流。

## 钱包评分（0-100）

每个钱包同步后会自动计算并存入 `wallet_score` 表，列表和 API 都会返回：

| 维度 | 分值 | 计算方式 |
|---|---:|---|
| 盈利 | 30 | 15 分 GMGN 全周期已实现利润（对数），15 分 7D/全周期收益率 |
| 胜率 | 20 | `win_sells / (win_sells + loss_sells)`，30%→0，70%→满分 |
| 稳定 | 20 | 正收益天数比例 10 分 + 盈亏比（gains/abs(losses)）10 分 |
| 社交 | 15 | 被追踪 `follow_count` 10 分 + 被备注 `remark_count` 5 分（对数） |
| 风险 | 15 | `1 - 加权风险比率`（钓鱼、不买持有、卖出大于买入、秒买/卖） |

- 数据窗口：最近 `SCORE_WINDOW_DAYS` 天（默认 90）的 `daily_profit` + `wallet_meta`
- 样本不足 30 天时，胜率和稳定分按置信度折减（最低 50%），避免一两天数据打出高分
- 评级：`A ≥ 80`、`B ≥ 60`、`C ≥ 40`、`D < 40`
- meta 未抓取时，社交/风险/收益率部分为 0，API 的 `score.meta_available`（`metrics.meta_available` 同值）会是 `false`，列表里分数旁会显示 `*`，同步后自动更新
- 历史钱包在服务启动后由后台任务补算；新钱包在同步时计算
- 单地址查询（`/api/profit/<address>`、详情页、盈利页）会按 `META_LIVE_CACHE_SECONDS` 缓存实时刷新 meta，并立即重算评分
- 列表接口使用后台元数据队列的缓存结果；队列独立于盈利同步，按 `META_TICK_SECONDS` / `META_BATCH_SIZE` 持续刷新

## 对外 API（无需登录，直接调）

线上服务地址为 `https://fnl.bquant.com`，所有路径都挂在它之下，例如 `https://fnl.bquant.com/api/wallets`。
（本地自测时为 `http://127.0.0.1:8038`，端口由 `PORT` 决定，默认 8038。）
这一组接口全部开放，**不需要登录、不要带 Cookie 或 token**。

### 状态返回合约（查询单地址时）

调用 `GET /api/profit/<address>` 时，外部系统会得到以下状态之一（HTTP 均为 200，用 `status` 字段区分）：

| Status | 语义 | 返回数据 | 外部系统该怎么办 |
|--------|------|----------|--------------|
| `ok` | 地址存在且有盈利数据 | `summary` + `days[]` | 直接使用数据渲染 |
| `pending` | 地址已登记但数据尚未就绪 | `message` + `retry_after_seconds` | 建议间隔后重新查询 |
| `not_found` | 地址未在跟踪列表中 | `action` 字段明确指示调用 POST /api/track | 按 `action` 调用 POST /api/track 登记地址 |
| `invalid_address` | 地址格式非法 | 错误说明 | 检查地址格式（32-44 个 base58 字符） |

**关键提示**：判断分支时务必用 `status` 字段而非只看 `success`，确保正确处理四种状态。

### 通用约定

- POST 接口必须带请求头 `Content-Type: application/json`，请求体是一个 JSON 对象；GET 接口用 query 参数传月份。
- 所有响应都是 JSON，其中 `success` 是布尔值表示成败。**失败时 HTTP 状态码仍是 200**，请读 `message` 字段拿具体原因，不要只看状态码。
- 地址校验：base58 编码、32–44 个字符的 Solana 地址。

### 接口列表

| 方法 | 端点 | 用途 | 请求参数 |
|---|---|---|---|
| POST | `/api/wallets` | 只把地址登记进跟踪列表，**不抓取**任何数据。适合先批量收地址、之后再统一同步；地址已存在时只更新非空 label，并保留原有启用状态 | body: `address` (必填) · `label` (可选) |
| POST | `/api/track` | 登记地址并**立即抓取**历史数据入库，返回抓取结果和可分享的盈利页地址。抓取耗时较长 | body: `address` (必填) · `label` (可选) · `months` (可选, 如 `["2026-10"]`) |
| GET | `/api/wallets` | 分页列出跟踪地址及其 label、meta、score，可用来核对登记结果。默认/最多 100 个/页 | query: `limit` (可选, 默认 100, 最大 100) · `offset` (可选, 默认 0) |
| GET | `/api/profit?month=2026-09` | 分页列出**已启用**地址该月汇总，每个地址一行（含 meta/score）；`total_pnl` 是所有启用地址的合计。不传 month 则取当月 | query: `month` (可选) · `limit` (可选, 默认/最大 100) · `offset` (可选) |
| GET | `/api/profit/<address>?month=2026-09` | 单个地址的每日明细，`days[]` 按天给买入/卖出、转入/转出的笔数与金额、胜负单数和已实现盈亏，外加 `summary`、`meta`、`score`。默认实时刷新 meta（60 秒缓存），`refresh=0` 可跳过 | path: `<address>` · query: `month` (可选) · `refresh` (可选, 默认 1) |
| GET | `/profit/<address>?month=2026-09` | 同一份数据的网页版，浏览器直接打开即可看图表，链接可分享给别人 | path: `<address>` · query: `month` (可选) |
| GET | `/api/health` | 健康检查，返回 `{"success": true, "service": "gmgndaily"}`，可用于探活 | 无 |

### 请求参数详解（两个 POST 接口通用）

- `address` **（必填）** — Solana 地址，base58 编码、32–44 个字符。缺失或为空报 `address required`，格式不对报 `invalid solana address`。
- `label`（可选）— 这个地址的备注名，例如「聪明钱01」，会显示在网页和汇总里。不传则保留该地址已有的备注（新地址为空）。
- `months`（仅 `/api/track`，可选）— 要抓哪几个月，数组，元素形如 `"2026-10"`。不传则按系统配置抓当月 + 往前 N 个月。

### 调用示例

```bash
# 1) 登记地址，不抓数据
curl -X POST https://fnl.bquant.com/api/wallets \
  -H "Content-Type: application/json" \
  -d '{"address":"Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN","label":"聪明钱01"}'

# 2) 登记地址 + 立即抓取入库
curl -X POST https://fnl.bquant.com/api/track \
  -H "Content-Type: application/json" \
  -d '{"address":"Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN","label":"聪明钱01","months":["2026-10"]}'

# 3) 查看地址列表（分页，默认/最多 100 个/页）
curl "https://fnl.bquant.com/api/wallets?limit=100&offset=0"

# 4) 地址当月汇总（分页） / 单地址每日明细
curl "https://fnl.bquant.com/api/profit?month=2026-10&limit=100&offset=0"
curl "https://fnl.bquant.com/api/profit/Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN?month=2026-10"

# 5) 健康检查
curl https://fnl.bquant.com/api/health
```

### 返回示例

- `POST /api/wallets` → `{"success": true, "wallet": {"address": "...", "label": "...", "enabled": 1, "created_at": "...", "updated_at": "..."}}`
- `POST /api/track` → `{"success": true, "wallet": "...", "added": true, "months": ["2026-10"], "results": [{"month": "2026-10", "days": 21, "pnl": 123.4, "source": "gmgn"}], "summary": {...}, "profit_url": "/profit/...?month=2026-10"}`，其中 `added` 表示这次是不是新加的地址，`results` 是每月抓取结果。若所有月份都抓取失败，`success` 为 `false` 且 `message` 以 `wallet saved but sync failed` 开头（地址仍会保留）。
- `GET /api/wallets?limit=100&offset=0` → `{"success": true, "wallets": [...], "pagination": {"limit": 100, "offset": 0, "total": 15276, "returned": 100, "has_more": true}}`
- `GET /api/profit?month=2026-10&limit=100&offset=0` → `{"success": true, "month": "2026-10", "total_pnl": 1234.5, "wallets": [...], "pagination": {...}}`
- `GET /api/profit/<address>` → `{"success": true, "wallet": "...", "month": "2026-10", "summary": {...}, "days": [...], "meta": {...}, "score": {...}}`
- 钱包相关的返回（`/api/wallets`、`/api/profit`、`/api/profit/<address>`、`/api/track`）都带 `meta`：
  `follow_count`（被追踪）、`remark_count`（被备注）、`tags`、`realized_profit_7d`（USD）、`pnl_7d`（比例）、
  `buy_7d`/`sell_7d`、`balance`（SOL）、`risk`、`has_data`、`fetched_at`、`last_error`。
  数据直接来自 GMGN 公开接口，未抓取到时 `meta` 为 `null`；完整字段见 [docs/API.md](./docs/API.md)。
  注意：API 行内的 `pnl_7d` 是本地按日汇总；GMGN 的 7D 统计在 `meta.realized_profit_7d`（USD）和 `meta.pnl_7d`（收益率）。
- 钱包相关的返回同时带 `score`：`value`（0-100）、`grade`、`window_days`、`meta_available`、`components`（五项得分）、`metrics`（原始指标）、`updated_at`。

单地址的 `days[]` 每个元素按 GMGN 每日记录存储，字段为：
`wallet, date, buy_count, sell_count, transfer_in_count, transfer_out_count, buy_volume, sell_volume, transfer_in_amount_usd, transfer_out_amount_usd, realized_profit, gains, losses, win_sells, loss_sells, last_trade_time`
（字段与 GMGN 原始字段的对应关系见 [gmgn_client.py](app/gmgn_client.py) 的 `normalize()`；
`date` 为 UTC 秒级时间戳，GMGN 源没有 `last_trade_time`，该字段固定为 0）。

### 常见错误（success 为 false，看 message）

- `address required` — body 里没有 `address` 或值为空。
- `invalid solana address` — 地址不是合法的 Solana 地址（长度或字符集不对）。
- `body must be JSON` — 没带 `Content-Type: application/json`，或 body 不是合法 JSON。
- `months must be a list of YYYY-MM strings` — `months` 不是数组，或元素不是合法月份（省略或传 `[]` 时按默认月份处理）。
- `wallet saved but sync failed: ...` — 地址已经存进去了，只是这次抓取失败，可稍后重新调用 `/api/track` 或等定时任务重试。

**怎么选**：只想批量加地址选 `/api/wallets`（瞬间返回）；想加完马上拿到数据选 `/api/track`（会等待抓取完成）。拿到数据后用 `/api/profit` 系列读结果。

## 目录

```
app/
  app.py            # FastAPI：页面 + 导入 + 同步队列 + 对外 API
  gmgn_client.py    # GMGN daily_profits 客户端（当前唯一数据源）
  profit_source.py  # 数据源适配层（Debot 部分暂时停用）
  debot_client.py   # Debot 客户端（暂时停用，留作后续恢复）
  database.py       # SQLite：wallets / daily_profit / wallet_meta / sync_log / sync_state / config
  validators.py     # Solana 地址、YYYY-MM 校验
  templates/        # base, index, wallets, detail, profit, config, login
  static/style.css
docs/gmgn-daily.md  # GMGN 接口实测说明
docs/debot.md       # Debot 抓包样本（历史参考）
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBOT_SESSION_ID` | 空 | 暂未使用（Debot 源已暂停，保留给后续恢复） |
| `GMGN_PASSWORD` | 空 → 启动时随机生成 | 控制台密码；生产环境必须设置（compose 会强制要求） |
| `SESSION_SECRET` | 空 → 每次启动随机 | 会话签名密钥；不设则重启后登录态失效、多 worker 互不认 |
| `SYNC_INTERVAL_MIN` | `60` | 当月数据最短同步间隔（分钟） |
| `HISTORY_SYNC_INTERVAL_MIN` | `1440` | 历史月份最短同步间隔（分钟，默认 24h） |
| `META_SYNC_INTERVAL_MIN` | `60` | 钱包公开元数据（被追踪/被备注/7D 统计）最短刷新间隔 |
| `META_TICK_SECONDS` | `30` | 元数据队列每批之间的间隔 |
| `META_BATCH_SIZE` | `20` | 每批刷新多少个钱包（每个钱包 2 个 GET） |
| `META_LIVE_CACHE_SECONDS` | `60` | 单地址/详情页实时刷新 meta 的缓存时间 |
| `META_LIVE_TIMEOUT_SECONDS` | `10` | 单地址实时刷新的上游超时 |
| `META_LIVE_MAX_REQUESTS_PER_MINUTE` | `60` | 实时刷新全局每分钟最多请求数 |
| `SCORE_WINDOW_DAYS` | `90` | 钱包评分使用的日历回看天数 |
| `SYNC_MONTHS_BACK` | `2` | 除当月外再往前同步几个月 |
| `SYNC_TICK_SECONDS` | `60` | 调度队列每批之间休眠的秒数 |
| `SYNC_BATCH_SIZE` | `60` | 每批最多处理多少个钱包（还受请求预算限制） |
| `SYNC_MAX_REQUESTS_PER_TICK` | `60` | 每批最多发起多少个 GMGN 请求 |
| `SYNC_LOG_RETENTION_DAYS` | `7` | `sync_log` 保留天数（过期自动清理） |
| `TRACK_MAX_CONCURRENT` | `4` | `/api/track` 同时抓取的最大并发数 |
| `DATABASE_PATH` | `/data/gmgndaily.db` | SQLite 路径 |
| `CHAIN` | `solana` | 链参数；GMGN 客户端会自动把 `solana` 转成 `sol` |
