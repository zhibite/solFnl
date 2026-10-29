# GMGN daily_profits 接口（当前实际使用）

> 最后更新：2026-10-28。线上**固定使用 GMGN**，Debot 数据源已暂停。
> 本文以 `app/gmgn_client.py` 的实际实现为准。

## 一、我们实际调用的接口

只有一个：

```http
POST https://gmgn.ai/pf/api/v1/wallets/sol/daily_profits?tz_name=Asia/Shanghai&tz_offset=28800
Content-Type: application/json
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Referer: https://gmgn.ai/
Origin: https://gmgn.ai

{
  "wallet_addresses": ["215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"],
  "start_at": 1788220800,
  "end_at":   1790812799
}
```

curl 等价写法：

```bash
curl -X POST 'https://gmgn.ai/pf/api/v1/wallets/sol/daily_profits?tz_name=Asia/Shanghai&tz_offset=28800' \
  -H 'Content-Type: application/json' \
  -H 'Referer: https://gmgn.ai/' \
  -H 'Origin: https://gmgn.ai' \
  -d '{"wallet_addresses":["215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"],"start_at":1788220800,"end_at":1790812799}'
```

调用链：

```
app.py  sync_one_wallet()
  └─ profit_source.GMGNSource.fetch_month(wallet, "YYYY-MM")
       └─ gmgn_client.GMGNDailyClient.fetch_daily(wallet, start_at, end_at)
            └─ POST /pf/api/v1/wallets/sol/daily_profits
```

要点：

- **不带 Cookie、token 或任何 query 参数**。
- `chain` 固定用 `sol`；客户端会把配置里的 `solana` 自动归一化成 `sol`。
- **一次只查一个钱包**。`wallet_addresses` 传多个地址时，服务端会把盈亏求和合并成一份，
  响应里也不带地址标识，无法拆分。
- `start_at` 必填，`end_at` 可选（省略则取到今天）。
- 时间跨度必须 **小于 1 年**，否则返回 `40000301 time range must be less than 1 year`。
- 当前月把 `end_at` 设为未来月末（如 10-31 23:59:59）实测正常，服务端会截到当天。

## 二、浏览器抓到的 URL 和实际请求的关系

从浏览器 DevTools 复制出来的 URL 通常长这样：

```
https://gmgn.ai/pf/api/v1/wallets/sol/daily_profits
  ?device_id=...&tab_id=...&fp_did=...&client_id=...
  &from_app=gmgn&app_ver=...&tz_name=Asia_Shanghai&tz_offset=28800
  &app_lang=zh-CN&os=web&worker=0
```

但要注意：

| 现象 | 实测结果 |
|---|---|
| 用这个 URL 发 GET（带浏览器头） | `404 page not found` |
| 用这个 URL 发 GET（不带浏览器头） | Cloudflare `403` 拦截页 |
| 用同样的路径发 POST + JSON body | `200`，`code: 0` |
| 带上/不带上面那串 query 参数 POST | 返回内容完全相同（实测响应 md5 一致） |
| 钱包地址在哪里 | 不在 URL 里，在 POST body 的 `wallet_addresses` |

结论：浏览器抓包时复制的只是 URL，**实际请求是 POST，钱包地址在 body 里**；
那串 `device_id / fp_did / tz_name ...` 都是可选的前端标识，我们的客户端不发送。

## 三、响应与字段字典

```json
{
  "code": 0,
  "reason": "",
  "message": "success",
  "data": {
    "list": [
      {
        "date": 1790812800,
        "total_profit": "27686.01876755388696498647",
        "total_buys": 3192,
        "total_sells": 3186,
        "total_transfer_ins": 516,
        "total_transfer_outs": 0,
        "buy_amount_usd": "878707.88955879398290654682",
        "sell_amount_usd": "908563.35947402332295534482",
        "transfer_in_amount_usd": "31915.25194852506356579778",
        "transfer_out_amount_usd": "0",
        "win_sells": 1601,
        "loss_sells": 1585,
        "win_profit": "82295.95320490874054763786",
        "loss_profit": "-54609.93443735485358265139"
      }
    ]
  }
}
```

字段全部入库，映射关系（见 `gmgn_client.normalize()`）：

| GMGN 字段 | 含义 | 入库列 |
|---|---|---|
| `date` | 日期标签（北京自然日编码为 UTC 零点） | `daily_profit.date` |
| `total_profit` | 当日已实现盈亏（字符串） | `realized_profit` |
| `total_buys` / `total_sells` | 买入 / 卖出笔数 | `buy_count` / `sell_count` |
| `total_transfer_ins` / `total_transfer_outs` | 转入 / 转出笔数 | `transfer_in_count` / `transfer_out_count` |
| `buy_amount_usd` / `sell_amount_usd` | 买入 / 卖出金额（USD） | `buy_volume` / `sell_volume` |
| `transfer_in_amount_usd` / `transfer_out_amount_usd` | 转入 / 转出金额（USD） | `transfer_in_amount_usd` / `transfer_out_amount_usd` |
| `win_sells` / `loss_sells` | 盈利单数 / 亏损单数 | `win_sells` / `loss_sells` |
| `win_profit` / `loss_profit` | 盈利合计 / 亏损合计（`loss_profit` 为负） | `gains` / `losses` |

补充说明：

- `date` 是**日期标签**：行内的 `2026-10-29` 表示北京时间 10-29 00:00–23:59，时间戳编码为该日期的 UTC 零点。
  每日分桶由 query 的 `tz_name` / `tz_offset` 决定（GMGN 网页同样传 `Asia/Shanghai` + `28800`），
  客户端在 `gmgn_client.py` 里固定带上这两个参数（可用 `GMGN_TZ_NAME` / `GMGN_TZ_OFFSET` 覆盖）。
  数据库按月查询仍用日期标签的 UTC 边界。
- 从 UTC 分桶切到北京分桶时，当前月 + 前 `SYNC_MONTHS_BACK` 个月的数据会在各钱包下次同步时被覆盖；
  更早月份不会自动重刷，仍保留 UTC 分桶（历史数据如需统一，需手动重同步）。
- 金额字段原样是字符串，入库时转成 SQLite `REAL`；展示到美分足够，若要做精确对账需要保留原始字符串。
- 响应里没有钱包地址字段，所以必须一个钱包一个请求。
- `daily_profit` 表里还有一列 `last_trade_time`（Debot 遗留），GMGN 源固定写 `0`。

## 四、钱包公开统计接口（被追踪 / 被备注 / 7D）

同步钱包时我们还会额外调用两个公开接口（无需登录）：

```http
GET https://gmgn.ai/api/v1/wallet_common_stat/sol/<wallet>
GET https://gmgn.ai/defi/quotation/v1/smartmoney/sol/wallet/<wallet>
```

`wallet_common_stat` 返回：

```json
{
  "follow_count": 5364,
  "remark_count": 3528,
  "tags": ["top_followed"],
  "tag_rank": {"top_followed": 0},
  "created_at": 1762440674,
  "fund_from_address": "F6DxYFix..."
}
```

`smartmoney/wallet` 返回：

```json
{
  "realized_profit_7d": 31140.98,
  "pnl_7d": 0.0171,
  "buy_7d": 7959,
  "sell_7d": 7975,
  "balance": "117.467",
  "total_profit": 2245598.91,
  "total_profit_pnl": 0.0278,
  "all_pnl": 0.0278,
  "risk": {"token_honeypot": "0", "...": "..."}
}
```

- `follow_count` 对应钱包页「被追踪」，`remark_count` 对应「被备注」
- `realized_profit_7d` 单位是 **USD**（前端按 SOL 价格换算后显示为 +303.82 SOL）
- `pnl_7d` 是收益率小数（`0.0171` = 1.71%）
- 这些数据存进 `wallet_meta` 表，外部 API 以 `meta` 对象返回

需要登录态的接口：

```http
GET https://gmgn.ai/pf/api/v1/wallet/sol/<wallet>/profit_stat/7d
```

未带 token 返回 `401 empty token`。前端 PnL 卡片会把 `wallet_common_stat` 和这个接口合并，
从中取 `total_fee` / `total_fee_usd` / `gas_cost` / `avg_holding_peroid` /
`history_bought_cost` / `total_volume` 等。**7D 手续费目前没有接入**，因为需要 GMGN 登录 token。

探测脚本：[`tools/probe_gmgn_wallet.py`](../tools/probe_gmgn_wallet.py)

## 五、实测行为与边界

| 行为 | 实测结果 |
|---|---|
| 方法 | **POST**；GET 不是可用接口（404 / 403） |
| 鉴权 | **不需要**；无 token、Cookie、公共 query 均可 |
| 公共 query | 非必需，可省略 |
| `start_at` | 必填 |
| `end_at` | 可选；省略取到今天 |
| 时间跨度 | 必须 < 1 年 |
| 多钱包 | **求和合并**，必须逐个钱包查 |
| 不存在的地址 | 不报错，返回该区间的空壳记录（`total_profit=0`） |
| 未来 `end_at` | 当前月传月末 23:59:59 正常，服务端截到当天 |
| 当日数据 | 盘中会持续变化；线上同步时间不同，当日数值可能和直查不同 |
| 失败响应 | 非 0 `code` 或 HTTP 错误由 `gmgn_client.GMGNError` 抛出 |

2026-10-28 在 `3.252.79.139` 上再次验证：

- 用抓包钱包 `Ar2Y6o1QmrRAskjii1cRfijeKugHH13ycxW5cd7rro1x` 请求 2026-10，
  首日 `27686.01876755388696498647`、`3192` 买 / `3186` 卖、`1601` 盈利单 / `1585` 亏损单，
  与浏览器抓包**逐字段完全一致**。
- 2026-09 请求返回 30 天；2026-10 请求返回 28 天（截至当天）。
- 线上该地址 10 月合计与 GMGN 直查只差当天：线上最后一次同步为 `2,369.92`，
  GMGN 当前值为 `4,021.50`，说明接口数据是实时变化的，不是历史数据错误。

## 六、与 Debot 的差异（历史参考）

Debot 数据源目前已暂停，以下对比仅作为历史口径参考。同一钱包 2026-09 的对比：

- 月度合计：Debot `144,165.22`，GMGN `141,132.30`，差异约 `-2.10%`。
- 逐日走势高度吻合，但每天有几十到几千美元的差异（买卖笔数判定、成本法处理不同）。
- GMGN 独有 `transfer_in/out`（转入转出）字段，Debot 没有。
- GMGN 不需要登录态，不存在 Cookie 过期问题；限制是时间跨度 < 1 年、必须逐钱包查询。

`debot_client.py` 和 `profit_source.py` 里的 Debot 代码保留，但 `_source_manager()`
固定 `enable_debot=False`，同步不会再走 Debot。

## 七、代码与工具

- 客户端实现：`app/gmgn_client.py`
  - `GMGNDailyClient.fetch_daily(wallet, start_at, end_at)`
  - `GMGNDailyClient.fetch_daily_many(wallets, start_at, end_at)`（内部串行逐个查）
  - `normalize(item)`：GMGN 字段 → 内部字段
  - `fetch_wallet_meta(wallet, chain)`：合并 `wallet_common_stat` + `smartmoney`
- 月份适配：`app/profit_source.py` 的 `GMGNSource.fetch_month(wallet, "YYYY-MM")`
- 同步入口：`app/app.py` 的 `sync_one_wallet()` / `sync_wallet_meta()`
- 探测脚本（历史，非生产）：`tools/probe_gmgn_daily.py` ~ `tools/probe_gmgn_daily8.py`
- 钱包元数据探测：`tools/probe_gmgn_wallet.py`
- 冒烟/验证脚本：`tools/test_gmgn_client.py`、`tools/verify_server_clients.py`

## 八、探测历史（简）

| 脚本 | 结论 |
|---|---|
| `probe_gmgn_daily.py` | GET 各种地址传法 → 全 404 |
| `probe_gmgn_daily2.py` | 路径枚举 + 抓前端 bundle 找接口常量 |
| `probe_gmgn_daily3b.py` | hybrid 路径实测 → 404 / 需要 token |
| `probe_gmgn_daily4.py` | 从 bundle 发现是 POST |
| `probe_gmgn_daily5.py` | POST 实测打通 `/pf/api/v1/wallets/sol/daily_profits` |
| `probe_gmgn_daily6.py` | 参数边界 + 鉴权确认 |
| `probe_gmgn_daily7.py` | 多钱包合并行为 + 与 Debot 对比 |
| `probe_gmgn_daily8.py` | 求和合并确证 + 接口一致性 |
