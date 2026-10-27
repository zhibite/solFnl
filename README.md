# gmgndaily — debot sol 钱包日盈利爬虫

定时抓取 [debot.ai](https://debot.ai) 钱包日历接口（`GET /api/dashboard/wallet/calendar`），
把每个 sol 地址的**每日盈利**存进 SQLite，前端可看、可导入地址，对外提供 HTTP API。

> 网络要求：debot 按 IP 地理位置封锁大陆出口，**必须跑在海外机器上**
> （已验证 `3.252.79.139` 可通）。接口需要登录态：浏览器登录 debot 后，
> 把 Cookie 里 `beegosessionID` 的值填到配置里。

## 快速启动（docker）

```bash
cp .env.example .env        # 填 DEBOT_SESSION_ID / GMGN_PASSWORD
docker compose up -d --build
# 打开 http://服务器IP:8038 ，用户名 zengsw 登录
```

## 本地开发

```bash
cd app
pip install -r requirements.txt
set DATABASE_PATH=./data.db & set DEBOT_SESSION_ID=你的beegosessionID
python app.py               # http://127.0.0.1:8038
```

## 用法

1. 登录控制台（`/`、`/wallets`、`/config` 需登录，用户名 `zengsw`）。
2. `/wallets` 页粘贴 JSON 或上传文件导入地址：
   `[{"address":"...","label":"聪明钱01"}]`，点“立即同步”。
3. `beegosessionID` 过期后去 `/config` 页粘贴新的（或改 `.env` 重启）。
4. 定时任务每 `SYNC_INTERVAL_MIN` 分钟自动同步当月 + 往前 N 个月。

## 对外 API（无需登录，直接调）

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET /api/wallets` | 地址列表 |
| `GET /api/profit?month=2026-09` | 全部地址月汇总（含总 PnL） |
| `GET /api/profit/<address>?month=2026-09` | 单地址每日明细（不带 month = 最近 500 天） |

单地址示例返回：`days[]` 每个元素字段与 `docs/debot.md` 抓包 1:1 对应
（`date, buy_count, sell_count, buy_volume, sell_volume, realized_profit, gains, losses, last_trade_time`），
外加 `summary` 月汇总。

## 目录

```
app/
  app.py            # FastAPI：页面 + 导入 + 定时同步 + 对外 API
  debot_client.py   # debot 日历接口客户端（带 beegosessionID Cookie）
  database.py       # SQLite：wallets / daily_profit / sync_log / config
  templates/        # base, index, wallets, detail, config, login
  static/style.css
docs/debot.md       # 接口抓包样本（字段字典）
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEBOT_SESSION_ID` | 空 | beegosessionID（也可存 DB，经 `/config` 页写入） |
| `GMGN_PASSWORD` | `anywn076034` | 控制台密码 |
| `SYNC_INTERVAL_MIN` | `60` | 同步间隔（分钟） |
| `SYNC_MONTHS_BACK` | `2` | 除当月外再往前同步几个月 |
| `DATABASE_PATH` | `/data/gmgndaily.db` | SQLite 路径 |
| `CHAIN` | `solana` | debot chain 参数 |
