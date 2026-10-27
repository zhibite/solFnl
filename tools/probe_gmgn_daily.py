#!/usr/bin/env python3
"""探测 GMGN daily_profits 接口的真实形态.

目标:
  1. /pf/api/v1/wallets/sol/daily_profits 到底怎么传钱包地址
     (path 段? query 参数? cookie? header?)
  2. 是否需要鉴权 / 哪些 query 参数是必须的
  3. 返回体结构, 条数, 时间范围

用法:
  python3 probe_gmgn_daily.py [wallet_address]
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import urllib.error

BASE = "https://gmgn.ai"

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

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://gmgn.ai/",
    "Origin": "https://gmgn.ai",
}

WALLETS = [
    "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP",   # debot 样本钱包
    "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ",   # gmgndata 里用过的
]


def fetch(url: str, headers=None, timeout=20):
    """返回 (status, body_text)."""
    req = urllib.request.Request(url, headers=headers or HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def summarize(body: str) -> str:
    """把响应体压成一行摘要."""
    body = body.strip()
    if not body:
        return "<empty>"
    try:
        j = json.loads(body)
    except Exception:
        return body[:180].replace("\n", " ")

    if isinstance(j, dict):
        code = j.get("code", j.get("status", "?"))
        msg = (j.get("message") or j.get("msg") or j.get("reason")
               or j.get("description") or "")
        data = j.get("data")
        extra = ""
        if isinstance(data, dict):
            lst = data.get("list") or data.get("calendar") or []
            if isinstance(lst, list) and lst:
                first, last = lst[0], lst[-1]
                extra = (f" list={len(lst)}"
                         f" date_range={first.get('date')}..{last.get('date')}")
        elif isinstance(data, list):
            extra = f" data_list={len(data)}"
        elif data is not None:
            extra = f" data_type={type(data).__name__}"
        return f"code={code} msg={str(msg)[:80]!r}{extra}"
    return body[:180].replace("\n", " ")


def probe(label: str, url: str, headers=None):
    status, body = fetch(url, headers)
    ok = "OK " if status == 200 else "   "
    print(f"{ok}[{status:>3}] {label}")
    print(f"        url: {url[:150]}")
    print(f"        -> {summarize(body)}")
    return status, body


def main() -> None:
    wallet = sys.argv[1] if len(sys.argv) > 1 else WALLETS[0]
    qs = urllib.parse.urlencode(COMMON_QS)
    print(f"wallet = {wallet}\n")

    print("=" * 78)
    print("A. 抓包原文 URL (path 里没有钱包地址)")
    print("=" * 78)
    probe("raw captured url",
          f"{BASE}/pf/api/v1/wallets/sol/daily_profits?{qs}")

    print()
    print("=" * 78)
    print("B. 钱包地址放 path 段")
    print("=" * 78)
    for path_tpl in [
        "/pf/api/v1/wallets/sol/{w}/daily_profits",
        "/pf/api/v1/wallets/sol/daily_profits/{w}",
        "/pf/api/v1/wallets/{w}/sol/daily_profits",
        "/defi/quotation/v1/wallets/sol/{w}/daily_profits",
    ]:
        probe(path_tpl, BASE + path_tpl.format(w=wallet) + "?" + qs)

    print()
    print("=" * 78)
    print("C. 钱包地址放 query 参数")
    print("=" * 78)
    for key in ["address", "wallet", "wallet_address", "addr", "account"]:
        q = COMMON_QS.copy()
        q[key] = wallet
        probe(f"?{key}=",
              f"{BASE}/pf/api/v1/wallets/sol/daily_profits?"
              + urllib.parse.urlencode(q))

    print()
    print("=" * 78)
    print("D. 去掉公共 query 参数, 只留地址 (看哪些参数是必须的)")
    print("=" * 78)
    probe("address only",
          f"{BASE}/pf/api/v1/wallets/sol/daily_profits?"
          + urllib.parse.urlencode({"address": wallet}))
    probe("address + device_id",
          f"{BASE}/pf/api/v1/wallets/sol/daily_profits?"
          + urllib.parse.urlencode({"address": wallet,
                                    "device_id": COMMON_QS["device_id"]}))
    probe("path address only (no qs)",
          f"{BASE}/pf/api/v1/wallets/sol/{wallet}/daily_profits")

    print()
    print("=" * 78)
    print("E. 附加时间/分页参数 (看能否拉更久的数据)")
    print("=" * 78)
    for extra in [
        {"limit": "365"},
        {"timeframe": "all"},
        {"days": "365"},
        {"period": "all"},
        {"range": "all"},
        {"start": "1500000000", "end": "1790000000"},
        {"type": "all"},
    ]:
        q = COMMON_QS.copy()
        q.update(extra)
        probe(str(extra),
              f"{BASE}/pf/api/v1/wallets/sol/daily_profits?"
              + urllib.parse.urlencode(q))

    print()
    print("=" * 78)
    print("F. 排行榜接口 (对照组, 已知可用)")
    print("=" * 78)
    for tf in ["1d", "7d", "30d"]:
        probe(f"rank {tf}",
              f"{BASE}/defi/quotation/v1/rank/sol/wallets/{tf}?" + qs)


if __name__ == "__main__":
    main()
