#!/usr/bin/env python3
"""验证服务器上 gmgndata 两个客户端都能正常导入并工作."""
import sys

sys.path.insert(0, ".")

from src.gmgn_client import GMGNClient            # noqa: E402  (排行榜)
from src.gmgn_daily_client import GMGNDailyClient  # noqa: E402  (单钱包日盈利)

print("[1] 排行榜客户端")
with GMGNClient() as c:
    items = c.fetch_rank("1d")
    print(f"    rank 1d -> {len(items)} 条, 首条地址 "
          f"{items[0].address[:10] if items else '-'}")

print("[2] 日盈利客户端")
d = GMGNDailyClient()
days = d.fetch_daily("215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP",
                     1785542400, 1790812800)
pnl = sum(x["total_profit"] for x in days)
print(f"    2026-08-01..2026-10-01 -> {len(days)} 天, 合计 pnl = {pnl:.2f}")

print("BOTH_OK")
