# GMGN daily_profits 接口（实测确认）

> 2026-10-27 在 `3.252.79.139` 上实测。结论：**这个接口可用，但原始抓包文档里的 URL 是错的。**

## 一、原始抓包文档的问题

`docs/gmgn-daily.md` 里记的 URL 是：

```
https://gmgn.ai/pf/api/v1/wallets/sol/daily_profits?device_id=...&tab_id=...
```

用这个 URL 发 **GET**，无论加不加公共 query 参数、不管钱包地址放 path 还是 query，
**一律返回 `404 page not found`**。之前 `gmgndata/_probe_daily3.py` 穷举了 144 条
候选路径全部落空，结论是「该接口已废弃」——这个结论是错的，错在**方法用错了**。

## 二、真实接口形态

```http
POST https://gmgn.ai/pf/api/v1/wallets/{chain}/daily_profits
Content-Type: application/json
Referer: https://gmgn.ai/
Origin: https://gmgn.ai/

{
  "wallet_addresses": ["215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"],
  "start_at": 1785542400,
  "end_at":   1790812800
}
```

响应：

```json
{
  "code": 0,
  "reason": "",
  "message": "success",
  "data": {
    "list": [ { "date": 1785542400, "total_profit": "1973.60...", ... } ]
  }
}
```

路径里的 chain 用 `sol`（`solana` 会报 `chain not supported`）。

### 关键行为

| 行为 | 实测结果 |
|---|---|
| 方法 | **POST**。GET 永远 404 |
| 鉴权 | **不需要**。不带 token、不带公共 query 参数都能拿到 `code=0` |
| 公共 query（`device_id` / `client_id` / `tab_id` …） | **非必需**，可省略 |
| `start_at` | **必填** |
| `end_at` | 可选，省略则取到今天 |
| 时间跨度 | 必须 **< 1 年**，否则 `40000301 time range must be less than 1 year` |
| 多钱包 | **会求和合并**！`wallet_addresses` 传多个地址，服务端把盈亏相加后返回一份，响应里也不带地址标识 → **必须逐个钱包查** |
| 不存在的地址 | 不报错，返回该日期区间的空壳记录（`total_profit=0`） |

### 字段字典

| 字段 | 含义 |
|---|---|
| `date` | unix 秒，UTC 零点 |
| `total_profit` | 当日已实现盈亏（字符串，需转 float） |
| `total_buys` / `total_sells` | 买入 / 卖出笔数 |
| `total_transfer_ins` / `total_transfer_outs` | 转入 / 转出笔数 |
| `buy_amount_usd` / `sell_amount_usd` | 买入 / 卖出金额（USD） |
| `transfer_in_amount_usd` / `transfer_out_amount_usd` | 转入 / 转出金额（USD） |
| `win_sells` / `loss_sells` | 盈利单数 / 亏损单数 |
| `win_profit` / `loss_profit` | 盈利合计 / 亏损合计（`loss_profit` 为负数） |

## 三、与 debot 的关系

同一钱包（`215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP`）2026-09 逐日对比：

- **口径基本一致**：月度合计 debot `144165.22` vs GMGN `141132.30`，差异 `-2.10%`
- 逐日走势高度吻合，但每天有几十到几千美元的小差异（买卖笔数也不同，如 09-06 debot 16/9 vs GMGN 12/9）→ 两家对「卖出笔」的判定和成本法处理不同
- GMGN 独有 `transfer_in/out`（转入转出）字段，debot 没有

**GMGN 这个接口可以替代 debot 作为数据源**，而且不需要登录态、不怕 cookie 过期。
唯一限制是**时间跨度 < 1 年**，且**必须逐个钱包查**。

## 四、怎么找到的

前端 bundle `/_app-*.js` 里直接写了请求函数：

```js
S = g => post({url:"/pf/api/v1/wallets/hybrid/daily_profits", authType:Access, data:g})
C = (g,b,y) => {
  if (isString(g)) {
    let _ = {wallet_addresses:b.wallet_addresses, start_at:b.start_at, end_at:b.end_at};
    return post({url:`/pf/api/v1/wallets/${g}/daily_profits`, authType: y?Access:None, data:_})
  } else {
    let _ = {chain:g, wallet_addresses:b.wallet_addresses,
             started_at:b.started_at, ended_at:b.ended_at, limit:b.limit||100};
    return post({url:"/api/v1/wallets/daily_performance/stats", authType:y?Access:None, data:_})
  }
}
```

三个候选路径里：

- `/pf/api/v1/wallets/{chain}/daily_profits` — **可用**（`authType` 可为 `None`）
- `/pf/api/v1/wallets/hybrid/daily_profits` — 需要 token（`40101611 empty token`）
- `/api/v1/wallets/daily_performance/stats` — 返回 `code=0` 但 `data` 为空，拿不到数据

## 五、代码

客户端：`app/gmgn_client.py`（本地）、`~/gmgndata/src/gmgn_daily_client.py`（服务器）

```python
from gmgn_client import GMGNDailyClient

c = GMGNDailyClient()                       # chain 默认 "sol"
days = c.fetch_daily(addr, start_ts, end_ts)          # 单个钱包
days_map = c.fetch_daily_many([a, b], start, end)     # 内部逐个查
```

服务器上另有一个等价版本 `~/gmgndata/src/gmgn_daily_client.py`，
与排行榜客户端 `~/gmgndata/src/gmgn_client.py` 并存互不影响。

## 六、探测脚本

`~/gmgndata/probe/` 下是完整的探测过程（8 轮，每轮都有 `.out` 记录）：

| 脚本 | 结论 |
|---|---|
| `probe_gmgn_daily.py` | GET 各种地址传法 → 全 404 |
| `probe_gmgn_daily2.py` | 路径枚举 + 抓 bundle 找接口常量 |
| `probe_gmgn_daily3b.py` | hybrid 路径实测 → 404 |
| `probe_gmgn_daily4.py` | 提取 bundle 里 `daily_profits` 上下文 → 发现是 POST |
| `probe_gmgn_daily5.py` | POST 实测 → 打通 `/pf/api/v1/wallets/sol/daily_profits` |
| `probe_gmgn_daily6.py` | 完整验证 + 参数边界 + 鉴权确认 |
| `probe_gmgn_daily7.py` | 多钱包合并行为 + 与 debot 对比 |
| `probe_gmgn_daily8.py` | 求和合并确证 + 接口一致性 |

冒烟测试：`~/gmgndata/test_gmgn_client.py`（`ALL_OK`）
双客户端验证：`~/gmgndata/verify_server_clients.py`（`BOTH_OK`）
