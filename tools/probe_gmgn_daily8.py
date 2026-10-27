#!/usr/bin/env python3
"""第八轮: 最终确认 + 边界.

确认 4 件事:
  1. 多钱包批量是「求和合并」(用 3 个钱包 + 不同查询顺序验证)
  2. /api/v1/wallets/daily_performance/stats 是否等价接口, 是否也合并
  3. daily_profits 与 daily_performance/stats 结果是否一致
  4. debot 与 GMGN 口径差异的量化说明 (跨多天)
"""
from __future__ import annotations

import gzip
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://gmgn.ai"
DAILY = "/pf/api/v1/wallets/sol/daily_profits"
STATS = "/api/v1/wallets/daily_performance/stats"

W1 = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
W2 = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"
W3 = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9xunG63N"

H = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://gmgn.ai/",
    "Origin": "https://gmgn.ai",
    "Accept-Encoding": "gzip, deflate",
}


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=H,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return resp.status, json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return -1, {"raw": f"{type(e).__name__}: {e}"}


def ts(d):
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def dstr(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def one_wallet(addr, rng):
    st, j = post(DAILY, {"wallet_addresses": [addr], **rng})
    return (j.get("data") or {}).get("list") or []


def part1_merge():
    print("=" * 78)
    print("1. 多钱包批量是否求和合并")
    print("=" * 78)
    rng = {"start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")}
    singles = {}
    for name, a in (("W1", W1), ("W2", W2), ("W3", W3)):
        lst = one_wallet(a, rng)
        singles[name] = {d["date"]: float(d["total_profit"]) for d in lst}
        print(f"  {name} 单独: {len(lst)} 天, "
              f"2026-09-01 total_profit = "
              f"{singles[name].get(ts('2026-09-01'))}")

    for combo in ([W1, W2], [W2, W1], [W1, W2, W3], [W3, W2, W1]):
        st, j = post(DAILY, {"wallet_addresses": combo, **rng})
        lst = (j.get("data") or {}).get("list") or []
        d0 = ts("2026-09-01")
        got = next((float(d["total_profit"]) for d in lst if d["date"] == d0), None)
        expect = sum(singles[n][d0] for n, a in
                     zip(["W1", "W2", "W3"], combo) if d0 in singles[n])
        label = "+".join(f"W{i+1}" for i in range(len(combo)))
        match = got is not None and abs(got - expect) < 1e-6
        print(f"  批量 [{label}] -> {got}   逐个相加 = {expect}   "
              f"{'== 确认求和合并' if match else '!='}")
        if len(lst) != 31:
            print(f"     (注意 list 长度 {len(lst)})")


def part2_stats():
    print()
    print("=" * 78)
    print("2. /api/v1/wallets/daily_performance/stats")
    print("=" * 78)
    rng = {"started_at": ts("2026-09-01"), "ended_at": ts("2026-10-01"),
           "limit": 100}
    for label, body in [
        ("单钱包", {"chain": "sol", "wallet_addresses": [W1], **rng}),
        ("双钱包", {"chain": "sol", "wallet_addresses": [W1, W2], **rng}),
        ("三钱包", {"chain": "sol", "wallet_addresses": [W1, W2, W3], **rng}),
    ]:
        st, j = post(STATS, body)
        data = j.get("data") or {}
        lst = data.get("list") or []
        d0 = ts("2026-09-01")
        got = next((float(d["total_profit"]) for d in lst if d["date"] == d0), None)
        print(f"  [{st}] {label}: code={j.get('code')} list={len(lst)} "
              f"data_keys={list(data.keys())}")
        print(f"       2026-09-01 total_profit = {got}")
        if lst:
            print(f"       首条字段 = {list(lst[0].keys())}")


def part3_consistency():
    print()
    print("=" * 78)
    print("3. daily_profits vs daily_performance/stats 一致性")
    print("=" * 78)
    a_rng = {"start_at": ts("2026-08-01"), "end_at": ts("2026-10-01")}
    st, j1 = post(DAILY, {"wallet_addresses": [W1], **a_rng})
    l1 = (j1.get("data") or {}).get("list") or []
    st, j2 = post(STATS, {"chain": "sol", "wallet_addresses": [W1],
                          "started_at": ts("2026-08-01"),
                          "ended_at": ts("2026-10-01"), "limit": 100})
    l2 = (j2.get("data") or {}).get("list") or []
    m1 = {d["date"]: d for d in l1}
    m2 = {d["date"]: d for d in l2}
    common = sorted(set(m1) & set(m2))
    print(f"  daily_profits {len(l1)} 天, stats {len(l2)} 天, 交集 {len(common)}")
    if common:
        diff = [(d, float(m1[d]["total_profit"]) - float(m2[d]["total_profit"]))
                for d in common]
        maxd = max(diff, key=lambda x: abs(x[1]))
        print(f"  total_profit 最大差 = {maxd[1]:.6f} (date {dstr(maxd[0])})")
        print("  -> " + ("两接口数据一致" if abs(maxd[1]) < 1e-6
                          else "两接口有细微差异"))


def part4_debot_gap():
    print()
    print("=" * 78)
    print("4. GMGN vs debot 口径差异 (2026-09, 同钱包)")
    print("=" * 78)
    st, j = post(DAILY, {"wallet_addresses": [W1],
                         "start_at": ts("2026-09-01"),
                         "end_at": ts("2026-10-01")})
    gm = {d["date"]: d for d in (j.get("data") or {}).get("list") or []}
    try:
        raw = open("docs/debot.md", encoding="utf-8-sig").read()
        db = json.loads(raw[raw.index("{"):])
        dbt = {d["date"]: d for d in db["data"]["calendar"]}
    except FileNotFoundError:
        print("  (无 docs/debot.md)")
        return
    common = sorted(set(gm) & set(dbt))
    print(f"  共同 {len(common)} 天")
    print(f"  {'date':<12}{'debot.pnl':>12}{'gmgn.pnl':>12}{'diff':>10}"
          f"{'db.buy/sell':>14}{'gm.buy/sell':>14}")
    for d0 in common:
        g, d = gm[d0], dbt[d0]
        print(f"  {dstr(d0):<12}{d['realized_profit']:>12.2f}"
              f"{float(g['total_profit']):>12.2f}"
              f"{float(g['total_profit']) - d['realized_profit']:>10.2f}"
              f"{str(d['buy_count']) + '/' + str(d['sell_count']):>14}"
              f"{str(g['total_buys']) + '/' + str(g['total_sells']):>14}")
    s1 = sum(dbt[d]["realized_profit"] for d in common)
    s2 = sum(float(gm[d]["total_profit"]) for d in common)
    print(f"  月度合计: debot={s1:.2f}  gmgn={s2:.2f}  "
          f"差异={(s2 - s1) / s1 * 100:.2f}%")


def main():
    part1_merge()
    part2_stats()
    part3_consistency()
    part4_debot_gap()


if __name__ == "__main__":
    main()
