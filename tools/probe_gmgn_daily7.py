#!/usr/bin/env python3
"""第七轮: 收尾验证.

要回答三个问题:
  Q1 多钱包批量时, list 里能不能区分是哪个钱包? (会不会只返第一个钱包)
  Q2 GMGN 的 total_profit 与 debot 的 realized_profit 口径是否一致?
  Q3 docs/gmgn-daily.md 那个样本 (URL 里没有钱包地址) 到底是哪个钱包的数据?
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
PATH = "/pf/api/v1/wallets/sol/daily_profits"
WALLET = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
WALLET2 = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"

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

COMMON_QS = {
    "device_id": "89546a76-a88d-4e75-9b81-af3aa735fb3a",
    "tab_id": "muy828z1rbaa",
    "fp_did": "25a3950ac1e6b7f401052bf149af4e2b",
    "client_id": "gmgn_web_20261006-5111-3972763",
    "from_app": "gmgn",
    "app_ver": "20261006-5111-3972763",
    "tz_name": "Asia_Shanghai",
    "tz_offset": "28800",
    "app_lang": "zh-CN",
    "os": "web",
    "worker": "0",
}


def post(path, body, qs=None):
    url = BASE + path
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=H, method="POST")
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


def get(path, qs=None):
    url = BASE + path
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, headers={**H, "Content-Type": None},
                                 method="GET")
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


def q1_multi_wallet():
    print("=" * 78)
    print("Q1. 多钱包批量: 响应里能否区分钱包?")
    print("=" * 78)
    rng = {"start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")}
    st, j = post(PATH, {"wallet_addresses": [WALLET], **rng})
    one = (j.get("data") or {}).get("list") or []
    print(f"  只查 W1        -> status={st} list={len(one)}")
    st2, j2 = post(PATH, {"wallet_addresses": [WALLET2], **rng})
    two = (j2.get("data") or {}).get("list") or []
    print(f"  只查 W2        -> status={st2} list={len(two)}")

    st3, j3 = post(PATH, {"wallet_addresses": [WALLET, WALLET2], **rng})
    lst = (j3.get("data") or {}).get("list") or []
    print(f"  同时查 W1+W2   -> status={st3} list={len(lst)}")
    print(f"  data keys = {list((j3.get('data') or {}).keys())}")
    if lst:
        print(f"  首条字段 = {list(lst[0].keys())}")
        print(f"  首条 = {json.dumps(lst[0], ensure_ascii=False)[:200]}")

    by_date_one = {d["date"]: d for d in one}
    by_date_two = {d["date"]: d for d in two}
    by_date_all = {d["date"]: d for d in lst}
    if by_date_all:
        d0 = sorted(set(by_date_one) & set(by_date_all))[0] \
            if set(by_date_one) & set(by_date_all) else None
        if d0:
            a = by_date_one[d0]["total_profit"]
            b = by_date_two.get(d0, {}).get("total_profit")
            c = by_date_all[d0]["total_profit"]
            print(f"  对比 {dstr(d0)}: W1单独={a}  W2单独={b}  批量={c}")
            if str(a) == str(c):
                print("  -> 批量结果 == W1 单独结果 (只返了第一个钱包)")
            elif str(b) == str(c):
                print("  -> 批量结果 == W2 单独结果 (只返了最后一个钱包)")
            else:
                print("  -> 批量结果是合并/其它结构, 需要看完整 data")
                print(f"     完整 data keys: {list((j3.get('data') or {}).keys())}")
                for k, v in (j3.get("data") or {}).items():
                    if k != "list":
                        print(f"     data[{k}] = "
                              f"{json.dumps(v, ensure_ascii=False)[:400]}")


def q2_vs_debot():
    print()
    print("=" * 78)
    print("Q2. GMGN total_profit  vs  debot realized_profit (同一钱包同一月)")
    print("=" * 78)
    st, j = post(PATH, {"wallet_addresses": [WALLET],
                        "start_at": ts("2026-09-01"),
                        "end_at": ts("2026-10-01")})
    lst = (j.get("data") or {}).get("list") or []
    gmgn_sep = {d["date"]: d for d in lst}

    try:
        raw = open("docs/debot.md", encoding="utf-8-sig").read()
        debot = json.loads(raw[raw.index("{"):])
        cal = debot["data"]["calendar"]
        debot_sep = {d["date"]: d for d in cal}
        print(f"  debot 样本 {len(cal)} 天 {dstr(cal[0]['date'])}..{dstr(cal[-1]['date'])}")
        print(f"  gmgn  样本 {len(lst)} 天 "
              f"{dstr(lst[0]['date'])}..{dstr(lst[-1]['date'])}" if lst else "  gmgn  空")
        common = sorted(set(gmgn_sep) & set(debot_sep))
        print(f"  共同日期 {len(common)} 天")
        if common:
            print(f"  {'date':<12}{'debot.realized':>18}{'gmgn.total':>18}"
                  f"{'debot.gains':>15}{'gmgn.win':>18}")
            for d0 in common[:6]:
                g = gmgn_sep[d0]
                db_ = debot_sep[d0]
                print(f"  {dstr(d0):<12}{db_['realized_profit']:>18.2f}"
                      f"{float(g['total_profit']):>18.2f}"
                      f"{db_['gains']:>15.2f}"
                      f"{float(g['win_profit']):>18.2f}")
            s_db = sum(debot_sep[d]["realized_profit"] for d in common)
            s_gm = sum(float(gmgn_sep[d]["total_profit"]) for d in common)
            s_dbw = sum(debot_sep[d]["gains"] for d in common)
            s_gmw = sum(float(gmgn_sep[d]["win_profit"]) for d in common)
            print(f"  合计 realized={s_db:.2f} total_profit={s_gm:.2f} "
                  f"差={s_gm - s_db:.2f}")
            print(f"  合计 gains   ={s_dbw:.2f} win_profit ={s_gmw:.2f} "
                  f"差={s_gmw - s_dbw:.2f}")
            # 笔数对比
            for d0 in common[:3]:
                g = gmgn_sep[d0]
                db_ = debot_sep[d0]
                print(f"  {dstr(d0)}: debot buy/sell="
                      f"{db_['buy_count']}/{db_['sell_count']}  "
                      f"gmgn buys/sells={g['total_buys']}/{g['total_sells']}")
    except FileNotFoundError:
        print("  (没有 docs/debot.md, 跳过)")


def q3_find_doc_wallet():
    print()
    print("=" * 78)
    print("Q3. 定位 docs/gmgn-daily.md 样本对应的钱包")
    print("=" * 78)
    # 文档样本: 2026-08-01 total_buys=1268, total_profit=2381.38224684810000000002
    target_buys = 1268
    target_profit = 2381.3822468481
    print(f"  目标特征: 2026-08-01 buys={target_buys} "
          f"total_profit={target_profit}")

    # 从排行榜里找候选 (rank 1d 里有当天交易数)
    st, j = get("/defi/quotation/v1/rank/sol/wallets/1d", COMMON_QS)
    rank = (j.get("data") or {}).get("rank") or []
    print(f"  rank 1d 拿到 {len(rank)} 条")
    if rank:
        print(f"  rank 字段名 = {list(rank[0].keys())}")
        cand = []
        for it in rank:
            for k, v in it.items():
                if isinstance(v, (int, float)) and abs(v - target_buys) < 1:
                    cand.append((it.get("address"), k, v))
                    break
        print(f"  rank 1d 中 buys 命中 {target_buys} 的候选: {cand[:10]}")
        # 字段里带 30d/7d 的当日数也看一眼
        hits = []
        for it in rank:
            addr = it.get("address")
            for k, v in it.items():
                if isinstance(v, (int, float)) and v == target_buys:
                    hits.append((addr, k))
        print(f"  任意字段等于 {target_buys}: {hits[:10]}")

    # 直接用 daily_profits 反查候选: 拿 rank 前 30 个地址逐个查 2026-08-01
    print("  逐个查 rank 前 30 个地址在 2026-08-01 的 buys ...")
    addrs = [it.get("address") for it in rank if it.get("address")][:30]
    found = None
    for i in range(0, len(addrs), 10):
        chunk = addrs[i:i + 10]
        st2, j2 = post(PATH, {"wallet_addresses": chunk,
                              "start_at": ts("2026-08-01"),
                              "end_at": ts("2026-08-02")})
        lst = (j2.get("data") or {}).get("list") or []
        for d in lst:
            if d.get("total_buys") == target_buys:
                found = (chunk, d)
                break
        if found:
            break
    if found:
        print(f"  !! 命中: {json.dumps(found[1], ensure_ascii=False)[:300]}")
    else:
        print("  rank 前 30 里没命中 (样本钱包不一定在 1d 榜, "
              "或批量结果无法区分钱包)")


def main():
    q1_multi_wallet()
    q2_vs_debot()
    q3_find_doc_wallet()


if __name__ == "__main__":
    main()
