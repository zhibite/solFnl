# 对外 API 文档

本文件是控制台 `/config` 页面「对外 API 文档」区块的完整版，供第三方系统集成参考。

服务基于 FastAPI，源码在 `app/app.py`。当前数据源固定为 **GMGN**（Debot 源暂停），
每日字段按 GMGN `daily_profits` 返回的数据存储；字段映射见 `app/gmgn_client.py` 的 `normalize()`。

---

## 一、通用约定

- **基础地址**：线上服务地址为 `https://fnl.bquant.com`，所有路径都挂在它之下，例如 `https://fnl.bquant.com/api/wallets`。
  （本地自测时为 `http://127.0.0.1:8038`，端口由环境变量 `PORT` 决定，默认 `8038`。）
- **鉴权**：对外接口全部开放，**不需要登录、不要带 Cookie 或 token**，直接请求即可。
- **请求格式**：
  - POST 接口必须带请求头 `Content-Type: application/json`，请求体通常是 JSON 对象（`/api/wallets/batch` 也接受数组）。
  - GET 接口用 query 参数传月份等可选值。
- **返回格式**：统一返回 JSON，其中 `success` 是布尔值表示成败。
  **失败时 HTTP 状态码仍是 200**，请读 `message` 字段拿具体原因，不要只看状态码。
- **地址校验**：base58 编码、32–44 个字符的 Solana 地址，正则 `^[1-9A-HJ-NP-Za-km-z]{32,44}$`。

### 分页

`GET /api/wallets` 和 `GET /api/profit` 是分页接口：

- `limit`：默认 100，最大 100；传入超过 100 会按 100 处理
- `offset`：默认 0
- 响应带 `pagination`：
  ```json
  {"limit": 100, "offset": 0, "total": 15276, "returned": 100, "has_more": true}
  ```
- `GET /api/profit` 的 `total_pnl` 是**所有启用地址**的合计，不是当前页合计
- 需要更多数据时继续增大 `offset`；单地址详情用 `GET /api/profit/<address>`

### 批量接口

**`POST /api/wallets/batch`**：批量登记，不抓取，最多 500 个。

```json
{"wallets": [{"address": "Gavtn...", "label": "聪明钱01"}, "地址字符串也可以"]}
```

返回：`{"success": true, "added": 10, "updated": 2, "skipped": 0, "invalid": 1, "total": 13}`

**`POST /api/profit/batch`**：批量查询，默认最多 30 个地址（`MAX_BATCH_PROFIT_ADDRESSES` 可调）。

```json
{"addresses": ["Gavtn...", "..."], "month": "2026-10",
 "include_days": false, "include_details": false}
```

- `month` 省略时取当月
- `include_days=false`（默认）只返回 `summary/meta/score`；为 `true` 时额外返回每日明细
- `include_details=false`（默认）返回精简 `meta`（关注/备注/7D/余额等）和精简 `score`（value/grade 等）；为 `true` 时才返回完整的风险字段和评分明细
- 使用缓存的 meta/score，不会逐地址实时请求 GMGN；需要实时刷新请用单地址接口
- 结果中每个地址的 `status` 为 `ok / pending / not_found / invalid_address / error`
- 响应还包含 `requested`、`count`、`duplicates_removed`、`include_days`、`include_details` 和按状态汇总的 `summary`

## 状态返回（查询单地址时）

调用 `GET /api/profit/<address>` 时，外部方会得到以下四种状态之一（HTTP 均为 200，用 `status` 字段区分）：

| Status | `success` | 语义 | 包含字段 | 外部方该怎么办 |
|--------|-----------|------|---------|--------------|
| `ok` | `true` | 地址存在且有盈利数据 | `summary` + `days[]` + `meta` + `score` | 直接使用数据 |
| `pending` | `true` | 地址已登记但数据尚未就绪 | `retry_after_seconds` | 稍后重新查询（建议间隔见 `retry_after_seconds`） |
| `not_found` | `false` | 地址未在跟踪列表中 | `action` 字段含 POST 指令 | 按 `action` 指示调用 POST /api/track 登记该地址 |
| `invalid_address` | `false` | 地址或月份格式非法 | `message` 说明原因 | 检查地址（32-44 个 base58 字符）和月份（YYYY-MM 格式） |

**重要提示**：
- 判断返回结果时，**务必用 `status` 字段而不是只看 `success`**。这样即使 API 返回格式变化，外部方的分支逻辑仍能正确执行。
- `month` 参数必须是 `YYYY-MM` 格式（如 `2026-10`），不符合格式会返回 `invalid_address` 状态。

## 钱包 meta（公开统计）

`GET /api/wallets`、`GET /api/profit`、`GET /api/profit/<address>`、`POST /api/track` 会附带 `meta` 对象；`POST /api/profit/batch` 默认返回精简 meta（未抓取到时为 `null`）：

```json
{
  "follow_count": 5364,
  "remark_count": 3528,
  "tags": ["top_followed"],
  "tag_rank": {"top_followed": 0},
  "created_at": 1762440674,
  "fund_from_address": "F6DxYFix...",
  "realized_profit_7d": 31140.98,
  "pnl_7d": 0.0171,
  "buy_7d": 7959,
  "sell_7d": 7975,
  "balance": 117.47,
  "total_profit": 2245598.9,
  "total_profit_pnl": 0.0278,
  "all_pnl": 0.0278,
  "last_active_timestamp": 1793234792,
  "risk": {"token_honeypot": "0", "...": "..."},
  "has_data": 1,
  "fetched_at": "2026-10-29T00:00:00+00:00",
  "last_error": ""
}
```

| 字段 | 说明 |
|---|---|
| `follow_count` | 被追踪人数（GMGN 钱包页「被追踪」） |
| `remark_count` | 被备注人数（GMGN 钱包页「被备注」） |
| `tags` / `tag_rank` | GMGN 钱包标签 |
| `created_at` | GMGN 记录的创建时间（unix 秒） |
| `realized_profit_7d` | GMGN 7D 已实现盈亏，**USD** |
| `pnl_7d` | GMGN 7D 收益率，小数（`0.0171` = 1.71%） |
| `buy_7d` / `sell_7d` | GMGN 7D 买入 / 卖出笔数 |
| `balance` | 钱包余额，**SOL** |
| `total_profit` / `total_profit_pnl` / `all_pnl` | GMGN 全周期统计 |
| `last_active_timestamp` | 最近活跃时间（unix 秒） |
| `risk` | GMGN 钓鱼/风险检测字段 |
| `has_data` | 是否成功抓取过数据（1/0）；0 时评分把 meta 视为不可用 |
| `fetched_at` | 本服务最近一次抓取该 meta 的时间 |
| `last_error` | 最近一次抓取失败原因，成功时为空字符串 |

> 数据直接来自 GMGN 公开接口（`wallet_common_stat` + `smartmoney`），不是本地按日汇总。
> 单地址查询（`GET /api/profit/<address>` 和网页版）默认会实时刷新 meta（`refresh=0` 可跳过），
> 刷新结果按 `META_LIVE_CACHE_SECONDS`（默认 60 秒）缓存；列表接口使用后台队列的缓存结果。
> 没有 meta 时返回 `null`，稍后同步后会填充。

## 钱包评分 score（0-100）

钱包相关接口同时返回 `score`（未计算时为 `null`）：

```json
{
  "value": 66.2,
  "grade": "B",
  "window_days": 90,
  "meta_available": true,
  "components": {
    "profitability": {"score": 18.1, "max": 30},
    "win_rate": {"score": 10.1, "max": 20, "value": 0.5015},
    "consistency": {"score": 8.0, "max": 20},
    "social": {"score": 15.0, "max": 15},
    "risk": {"score": 15.0, "max": 15}
  },
  "metrics": {
    "days": 28, "net_pnl": 1234.5, "win_rate": 0.5015,
    "calendar_confidence": 0.933,
    "follow_count": 5364, "remark_count": 3528,
    "meta_available": true
  },
  "updated_at": "2026-10-29T00:00:00+00:00"
}
```

| 维度 | 分值 | 依据 |
|---|---:|---|
| 盈利 profitability | 30 | GMGN 全周期已实现利润（对数，15）+ 7D/全周期收益率（15） |
| 胜率 win_rate | 20 | `win_sells / (win_sells + loss_sells)`，30%→0，70%→满分 |
| 稳定 consistency | 20 | 正收益天数比例（10）+ 盈亏比（10） |
| 社交 social | 15 | 被追踪 `follow_count`（10）+ 被备注 `remark_count`（5） |
| 风险 risk | 15 | `1 - 加权风险比率`（钓鱼/不买持有/卖出大于买入/秒买） |

- 数据窗口：最近 `SCORE_WINDOW_DAYS` 天（默认 90）的 `daily_profit` + `wallet_meta`
- 样本不足 30 天时，胜率和稳定分按置信度折减（最低 50%）；`metrics.calendar_confidence` 给出置信度
- 评级：`A ≥ 80`、`B ≥ 60`、`C ≥ 40`、`D < 40`
- `meta_available=false`（`metrics.meta_available` 同值）表示 meta 还没抓到，社交/风险/收益率部分暂为 0，评分是临时值
- 评分在钱包同步、meta 刷新、单地址实时刷新后自动重算；历史钱包由后台任务补算

---

## 二、接口列表

| 方法 | 端点 | 用途 | 请求参数 |
|---|---|---|---|
| POST | `/api/wallets` | 只把地址登记进跟踪列表，**不抓取**任何数据。适合先批量收地址、之后再统一同步；地址已存在时只更新非空 label，并保留原有启用状态 | body: `address` (必填) · `label` (可选) |
| POST | `/api/track` | 登记地址并**立即抓取**历史数据入库，返回抓取结果、钱包 `meta`、评分 `score` 和可分享的盈利页地址。抓取耗时较长 | body: `address` (必填) · `label` (可选) · `months` (可选, 如 `["2026-10"]`) |
| POST | `/api/wallets/batch` | 批量登记地址，不抓取；最多 500 个/次 | body: `wallets` 或 `addresses` 数组，元素为地址字符串或 `{address,label,enabled}` |
| POST | `/api/profit/batch` | 批量查询最多 30 个地址某月汇总，返回每地址 `summary/meta/score` | body: `addresses` (必填, ≤30) · `month` (可选) · `include_days` (可选, 默认 false) · `include_details` (可选, 默认 false) |
| GET | `/api/wallets` | 分页列出跟踪地址及其 label、启用状态、创建时间、钱包 `meta` 和 `score`。默认/最多 100 个/页 | query: `limit` (可选, 默认 100, 最大 100) · `offset` (可选, 默认 0) |
| GET | `/api/profit?month=2026-10` | 分页列出**已启用**地址该月汇总，每个地址一行（含 `meta` 和 `score`）；`total_pnl` 是所有启用地址合计。不传 month 则取当月 | query: `month` (可选) · `limit` (可选, 默认/最大 100) · `offset` (可选) |
| GET | `/api/profit/<address>?month=2026-10` | **状态查询**（见下方"状态返回"表）：地址存在且有数据时返回每日明细 `days[]` + `summary` + `meta` + `score`；不传 month 则取最近 500 天；地址未被跟踪时返回添加指令 | path: `<address>` · query: `month` (可选) · `refresh` (可选, 默认 1) |
| GET | `/profit/<address>?month=2026-10` | 同一份数据的网页版，浏览器直接打开即可看图表，链接可分享给别人 | path: `<address>` · query: `month` (可选) |
| GET | `/api/health` | 健康检查，返回 `{"success": true, "service": "gmgndaily"}`，可用于探活 | 无 |

---

## 三、单地址 POST 接口参数（`/api/wallets`、`/api/track`）

| 参数 | 必填 | 说明 |
|---|---|---|
| `address` | 是 | Solana 地址，base58 编码、32–44 个字符。缺失或为空报 `address required`，格式不对报 `invalid solana address` |
| `label` | 否 | 这个地址的备注名，例如「聪明钱01」，会显示在网页和汇总里。不传则保留该地址已有的备注（新地址为空） |
| `months` | 否（仅 `/api/track`） | 要抓哪几个月，数组，元素形如 `"2026-10"`。不传则按系统配置抓当月 + 往前 N 个月（`SYNC_MONTHS_BACK`） |

---

## 四、调用示例（curl）

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

# 5) 批量登记（最多 500）
curl -X POST https://fnl.bquant.com/api/wallets/batch \
  -H "Content-Type: application/json" \
  -d '{"wallets":[{"address":"Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN","label":"聪明钱01"}]}'

# 6) 批量查询（默认最多 30，精简 meta/score）
curl -X POST https://fnl.bquant.com/api/profit/batch \
  -H "Content-Type: application/json" \
  -d '{"addresses":["Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN"],"month":"2026-10","include_details":false}'

# 7) 健康检查
curl https://fnl.bquant.com/api/health
```

Python 调用示例：

```python
import requests

BASE = "https://fnl.bquant.com"
ADDR = "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN"

# 只登记，不抓取
r = requests.post(f"{BASE}/api/wallets",
                  json={"address": ADDR, "label": "聪明钱01"})
print(r.json())

# 登记并抓取
r = requests.post(f"{BASE}/api/track",
                  json={"address": ADDR, "label": "聪明钱01", "months": ["2026-10"]})
print(r.json())

# 读月汇总
r = requests.get(f"{BASE}/api/profit",
                 params={"month": "2026-10", "limit": 100, "offset": 0})
print(r.json())
```

---

## 五、返回示例

**`POST /api/wallets`**

```json
{
  "success": true,
  "wallet": {
    "address": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
    "label": "聪明钱01",
    "enabled": 1,
    "created_at": "2026-10-28T12:13:50.287281+00:00",
    "updated_at": "2026-10-28T12:13:50.287281+00:00"
  },
  "meta": null,
  "score": null
}
```

**`POST /api/wallets/batch`**

```json
{
  "success": true,
  "added": 10,
  "updated": 2,
  "skipped": 0,
  "invalid": 1,
  "total": 13
}
```

**`POST /api/track`**

```json
{
  "success": true,
  "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
  "added": true,
  "months": ["2026-10"],
  "results": [
    {"month": "2026-10", "days": 21, "pnl": 123.4, "source": "gmgn"}
  ],
  "summary": { "...": "首个成功月份的汇总，结构同 GET /api/profit/<address> 的 summary" },
  "meta": { "...": "钱包公开统计，结构见上方「钱包 meta」；未抓取到时为 null" },
  "score": { "...": "钱包评分，结构见上方「钱包评分 score」；未计算时为 null" },
  "profit_url": "/profit/Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN?month=2026-10"
}
```

- `added` 表示这次是不是新加的地址（已存在则为 `false`）。
- `months` 是本次请求的月份列表；`results` 是按月的抓取结果，失败的月份会带 `error`。
- 所有月份都失败时 `success` 为 `false`，`message` 以 `wallet saved but sync failed` 开头（地址仍会保存）。
- `profit_url` 是相对路径，拼上基础地址即可在浏览器打开。

**`POST /api/profit/batch`**

```json
{
  "success": true,
  "month": "2026-10",
  "requested": 2,
  "count": 2,
  "duplicates_removed": 0,
  "include_days": false,
  "include_details": false,
  "summary": {"ok": 1, "pending": 0, "not_found": 1, "invalid_address": 0, "error": 0},
  "results": [
    {
      "address": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
      "status": "ok",
      "month": "2026-10",
      "summary": { "...": "同单地址 summary" },
      "meta": { "...": "钱包 meta" },
      "score": { "...": "钱包评分" }
    },
    {
      "address": "11111111111111111111111111111111",
      "status": "not_found",
      "message": "address not tracked yet, call POST /api/track to add it",
      "action": { "...": "同单地址 not_found 的 action" }
    }
  ]
}
```

- `include_days=true` 时每个 `ok` 结果还会带 `days[]`
- `include_details=true` 时 `meta/score` 才包含完整风险字段和评分明细；默认是精简字段
- 批量查询使用缓存的 meta/score；需要实时刷新请用单地址接口

**`GET /api/wallets`**

```json
{
  "success": true,
  "wallets": [
    {
      "address": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
      "label": "聪明钱01",
      "enabled": 1,
      "created_at": "2026-10-28T12:13:50.287281+00:00",
      "updated_at": "2026-10-28T12:13:50.287281+00:00",
      "meta": { "...": "结构见上方「钱包 meta」；未抓取到时为 null" },
      "score": { "...": "结构见上方「钱包评分 score」；未计算时为 null" }
    }
  ],
  "pagination": {"limit": 100, "offset": 0, "total": 15276, "returned": 100, "has_more": true}
}
```

**`GET /api/profit`**

```json
{
  "success": true,
  "month": "2026-10",
  "total_pnl": 1234.5,
  "wallets": [ {
    "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
    "label": "聪明钱01",
    "enabled": 1,
    "month": "2026-10",
    "days": 21,
    "realized_profit": 1234.5,
    "gains": 2100.0,
    "losses": 865.5,
    "buy_count": 88,
    "sell_count": 42,
    "buy_volume": 878707.89,
    "sell_volume": 908563.36,
    "transfer_in_count": 516,
    "transfer_out_count": 0,
    "transfer_in_amount_usd": 31915.25,
    "transfer_out_amount_usd": 0.0,
    "win_sells": 30,
    "loss_sells": 12,
    "win_rate": 0.7142857142857143,
    "pnl_7d": 320.0,
    "today_pnl": 45.6,
    "updated_at": "2026-10-28T12:13:50.287281+00:00",
    "meta": { "...": "结构见上方「钱包 meta」；未抓取到时为 null" },
    "score": { "...": "结构见上方「钱包评分 score」；未计算时为 null" }
  } ],
  "pagination": {"limit": 100, "offset": 0, "total": 15276, "returned": 100, "has_more": true}
}
```

每个钱包行是该地址该月的汇总；`buy_volume` / `sell_volume` / `transfer_*` 都是当月合计。
`today_pnl` 是当前北京时间自然日（UTC+8）的已实现盈亏（最近一次同步时的值），与 `month` 参数无关。
`meta` 是 GMGN 公开统计（被追踪/被备注/7D 盈利等），结构见上方「钱包 meta」；未抓取到时为 `null`。

> 行内的 `pnl_7d` 是本服务按日记录计算的近 7 天净盈亏；
> GMGN 的 7D 统计在 `meta.realized_profit_7d`（USD）和 `meta.pnl_7d`（收益率小数）。

**`GET /api/profit/<address>`**

状态 `ok`（有数据）：

```json
{
  "success": true,
  "status": "ok",
  "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
  "month": "2026-10",
  "summary": {
    "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
    "month": "2026-10",
    "days": 21,
    "realized_profit": 1234.5,
    "gains": 2100.0,
    "losses": 865.5,
    "buy_count": 88,
    "sell_count": 42,
    "win_sells": 30,
    "loss_sells": 12,
    "win_rate": 0.7142857142857143,
    "pnl_7d": 320.0,
    "today_pnl": 45.6
  },
  "meta": { "...": "结构见上方「钱包 meta」；未抓取到时为 null" },
  "score": { "...": "结构见上方「钱包评分 score」；未计算时为 null" },
  "days": [
    {
      "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
      "date": 1761955200,
      "buy_count": 3,
      "sell_count": 1,
      "transfer_in_count": 2,
      "transfer_out_count": 0,
      "buy_volume": 1200.5,
      "sell_volume": 900.25,
      "transfer_in_amount_usd": 150.0,
      "transfer_out_amount_usd": 0.0,
      "realized_profit": 300.25,
      "gains": 400.0,
      "losses": 99.75,
      "win_sells": 1,
      "loss_sells": 0,
      "last_trade_time": 1761993600
    }
  ]
}
```

状态 `pending`（地址已登记，数据还没抓到）：

```json
{
  "success": true,
  "status": "pending",
  "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
  "month": "2026-10",
  "message": "address is tracked but profit data is not available yet, it will appear after the next sync",
  "retry_after_seconds": 3600,
  "summary": null,
  "days": [],
  "meta": { "...": "结构见上方「钱包 meta」；未抓取到时为 null" },
  "score": { "...": "结构见上方「钱包评分 score」；未计算时为 null" }
}
```

状态 `not_found`（地址不在跟踪列表里）：

```json
{
  "success": false,
  "status": "not_found",
  "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
  "message": "address not tracked yet, call POST /api/track to add it",
  "action": {
    "method": "POST",
    "endpoint": "/api/track",
    "body": { "address": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN" },
    "description": "登记该地址并立即抓取历史盈利数据"
  }
}
```

状态 `invalid_address`（地址或月份格式非法）：

```json
{
  "success": false,
  "status": "invalid_address",
  "wallet": "Gavtn1EfSV7s3xvUoZruDCnyoVU94WT2KA7WSnAzdxzN",
  "message": "invalid solana address"
}
```

`days[]` 每个元素按 GMGN 每日记录存储，字段为：
`wallet, date, buy_count, sell_count, transfer_in_count, transfer_out_count, buy_volume, sell_volume, transfer_in_amount_usd, transfer_out_amount_usd, realized_profit, gains, losses, win_sells, loss_sells, last_trade_time`。
`date` 是日期标签：该天北京时间自然日的日期编码成 UTC 零点时间戳；GMGN 源没有 `last_trade_time`，该字段固定为 `0`；金额单位为 USD。
（GMGN 原始字段到这些字段的映射见 `app/gmgn_client.py` 的 `normalize()`。）

`summary.today_pnl` 是当前北京时间自然日（UTC+8）的已实现盈亏，与 `month` 参数无关，取最近一次同步的数据；
单地址、批量查询和 `/api/track` 的 `summary` 都会带这个字段。

**`GET /api/health`**

```json
{ "success": true, "service": "gmgndaily" }
```

---

## 六、常见错误（`success` 为 false，看 `message`）

| `message` | 原因 | 处理 |
|---|---|---|
| `address required` | body 里没有 `address` 或值为空 | 补上 `address` 字段 |
| `invalid solana address` | 地址不是合法的 Solana 地址（长度或字符集不对） | 核对地址，应为 base58、32–44 字符 |
| `invalid month format, use YYYY-MM` | `month` 参数格式不对 | 传 `2026-10` 这样的 `YYYY-MM` 格式，或直接省略该参数 |
| `body must be JSON` | 没带 `Content-Type: application/json`，或 body 不是合法 JSON | 补请求头，确认 body 是合法 JSON |
| `months must be a list of YYYY-MM strings` | `months` 不是数组，或元素不是合法月份 | 传 `["2026-10"]` 这样的数组；省略或传 `[]` 时按默认月份处理 |
| `addresses must be a non-empty list` | `/api/profit/batch` 地址数组为空或格式不对 | 传非空 `addresses` 数组，默认最多 30 个 |
| `too many addresses (max 30)` | 批量查询超过当前上限（默认 30） | 拆成多次请求，或调整 `MAX_BATCH_PROFIT_ADDRESSES` |
| `wallets must be a non-empty list` | `/api/wallets/batch` 地址数组为空或格式不对 | 传非空 `wallets`/`addresses` 数组，最多 500 个 |
| `too many wallets (max 500)` | 批量登记超过 500 个地址 | 拆成多次请求 |
| `wallet saved but sync failed: ...` | 地址已经存进去了，只是这次抓取失败 | 可稍后重新调用 `/api/track`，或等定时任务重试；`profit_url` 仍可用于查看已有数据 |

> 注意：`GET /api/profit/<address>` 的错误信息放在 `message` 字段，同时 `status` 会是
> `invalid_address` / `not_found` 两个值之一。`pending` 状态下 `success` 为 `true`，
> 因为请求本身是成功完成的，只是数据还没准备好。

---

## 七、怎么选接口

- 只想**批量加地址** → `POST /api/wallets/batch`（最多 500，瞬间返回，不触发抓取）；单个可用 `POST /api/wallets`。
- 想**加完马上拿数据** → `POST /api/track`（会等待抓取完成，耗时较长）。
- 想**一次查一批指定地址** → `POST /api/profit/batch`（默认最多 30，返回每月汇总/精简 meta/score）；要完整评分明细加 `"include_details": true`，要每日明细加 `"include_days": true`。
- 拿到数据后用 `GET /api/profit` 系列读结果：整体看 `GET /api/profit`，单地址看 `GET /api/profit/<address>`，要给人看就用 `GET /profit/<address>`（网页版）。

典型流程：

```
POST /api/track          登记 + 抓取
      ↓
GET  /api/profit         看全部地址当月汇总
GET  /api/profit/<addr>  看单个地址每日明细
```

单地址查询的状态处理示例（推荐外部方按 `status` 分支）：

```python
r = requests.get(f"{BASE}/api/profit/{ADDR}", params={"month": "2026-10"})
data = r.json()

if data["status"] == "ok":
    print("有数据:", data["summary"]["realized_profit"])
elif data["status"] == "pending":
    print(f"数据未就绪，{data['retry_after_seconds']} 秒后重试")
elif data["status"] == "not_found":
    # 按 action 字段的指示登记地址
    requests.post(f"{BASE}/api/track", json=data["action"]["body"])
else:  # invalid_address
    print("地址或月份格式错误:", data["message"])
```

---
