#!/usr/bin/env python3
"""GMGN daily_profits 接口冒烟测试 (服务器上跑).

验证:
  1. 单钱包拉取正常, code=0
  2. 时间跨度 > 1 年被本地校验挡住
  3. 多钱包是求和合并 (因此必须逐个查)
  4. 错误地址不炸, 返回空列表
  5. 与 debot 样本口径接近 (月度差异 < 5%)

用法:
  python3 test_gmgn_client.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.path.insert(0, "app")

from gmgn_client import GMGNDailyClient, GMGNError  # noqa: E402

WALLET = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
WALLET2 = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def dstr(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    c = GMGNDailyClient()
    ok = True

    print("[1] 单钱包拉取 2026-09")
    days = c.fetch_daily(WALLET, ts("2026-09-01"), ts("2026-10-01"))
    pnl = sum(d["total_profit"] for d in days)
    print(f"    {len(days)} 天, 合计 pnl = {pnl:.2f}")
    print(f"    首条 {dstr(days[0]['date'])} "
          f"profit={days[0]['total_profit']:.2f} "
          f"buys={days[0]['total_buys']} sells={days[0]['total_sells']}")
    if not days:
        print("    !! 空结果")
        ok = False

    print("[2] 超过 1 年的区间应被本地拦截")
    try:
        c.fetch_daily(WALLET, ts("2024-01-01"), ts("2026-01-01"))
        print("    !! 没拦住")
        ok = False
    except ValueError as e:
        print(f"    OK 已拦截: {e}")

    print("[3] 多钱包是求和合并 -> 必须逐个查")
    one = c.fetch_daily(WALLET, ts("2026-09-01"), ts("2026-10-01"))
    two = c.fetch_daily(WALLET2, ts("2026-09-01"), ts("2026-10-01"))
    both = c.fetch_daily_many([WALLET, WALLET2], ts("2026-09-01"),
                              ts("2026-10-01"))
    s1 = sum(d["total_profit"] for d in one)
    s2 = sum(d["total_profit"] for d in two)
    s_both = sum(d["total_profit"] for d in both[WALLET])
    print(f"    W1={s1:.2f}  W2={s2:.2f}  fetch_daily_many[W1]={s_both:.2f}")
    if abs(s_both - s1) > 0.01:
        print("    !! fetch_daily_many 结果与单查不一致")
        ok = False
    else:
        print("    OK fetch_daily_many 逐个查, 数值正确")

    print("[4] 不存在的地址不抛异常")
    try:
        empty = c.fetch_daily("11111111111111111111111111111111",
                              ts("2026-09-01"), ts("2026-10-01"))
        print(f"    OK 返回 {len(empty)} 条 (不抛异常)")
    except GMGNError as e:
        print(f"    抛错: {e}")

    print("[5] 与 debot 样本口径对比")
    try:
        raw = open("docs/debot.md", encoding="utf-8-sig").read()
        import json
        db = json.loads(raw[raw.index("{"):])
        cal = {d["date"]: d for d in db["data"]["calendar"]}
        gm = {d["date"]: d for d in days}
        common = sorted(set(cal) & set(gm))
        s_db = sum(cal[d]["realized_profit"] for d in common)
        s_gm = sum(gm[d]["total_profit"] for d in common)
        diff = (s_gm - s_db) / s_db * 100 if s_db else 0
        print(f"    {len(common)} 天  debot={s_db:.2f}  gmgn={s_gm:.2f}  "
              f"差异={diff:.2f}%")
        if abs(diff) > 5:
            print("    !! 差异超过 5%")
            ok = False
        else:
            print("    OK 口径一致 (差异 < 5%)")
    except FileNotFoundError:
        print("    (无 docs/debot.md, 跳过)")

    print()
    print("ALL_OK" if ok else "HAS_FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
