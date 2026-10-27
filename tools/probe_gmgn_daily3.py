#!/usr/bin/env python3
"""第三轮: 确认 /pf/api/v1/wallets/hybrid/daily_profits 的可用参数组合.

目标:
  1. 找到能返回 200 + code=0 的参数组合
  2. 确认地址怎么传 (query? path?), 需要哪些附加参数
  3. 看返回条数 / 时间范围, 能否拿全历史
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import urllib.error
import gzip
import io

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
    "Accept-Encoding": "gzip, deflate",
}

CANDIDATE_PATHS = [
    "/pf/api/v1/wallets/hybrid/daily_profits",
    "/pf/api/v1/wallets/hybrid/person_wallets_pnl_info",
    "/pf/api/v1/wallets/hybrid/refresh_balance",
    "/pf/api/v1/wallets/hybrid/wallet_holding_info",
    "/api/v1/wallets/daily_performance/stats",
]

ADDRESS_KEYS = ["address", "wallet", "wallet_address", "addresses"]


def fetch(url: str, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def brief(body: str) -> str:
    body = body.strip()
    if not body:
        return "<empty>"
    if body.startswith("<"):
        return "HTML:" + body[:100].replace("\n", " ")
    try:
        j = json.loads(body)
    except Exception:
        return body[:160].replace("\n", " ")
    if not isinstance(j, dict):
        return body[:160]
    code = j.get("code", j.get("status", "?"))
    msg = (j.get("message") or j.get("msg") or j.get("reason")
           or j.get("description") or "")
    data = j.get("data")
    extra = ""
    if isinstance(data, dict):
        extra = " keys=" + ",".join(list(data.keys())[:10])
        lst = None
        for k in ("list", "calendar", "rank", "history", "items", "daily_profits"):
            v = data.get(k)
            if isinstance(v, list) and v:
                lst = v
                extra = f" {k}={len(v)}"
                if isinstance(v[0], dict) and v[0].get("date"):
                    extra += (f" {v[0]['date']}..{v[-1].get('date')}")
                break
    elif isinstance(data, list):
        extra = f" data_list={len(data)}"
    return f"code={code} msg={str(msg)[:60]!r}{extra}"


def probe(label, url, quiet=False):
    st, body = fetch(url)
    ok = st == 200 and not body.lstrip().startswith("<")
    if ok or not quiet:
        print(f"{'OK ' if ok else '   '}[{st:>3}] {label} -> {brief(body)}")
    return st, body


def main():
    wallet = sys.argv[1] if len(sys.argv) > 1 else \
        "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
    qs = urllib.parse.urlencode(COMMON_QS)
    print(f"wallet = {wallet}\n")

    print("=" * 78)
    print("1. 裸路径 + 公共 query (看路由是否存在)")
    print("=" * 78)
    for p in CANDIDATE_PATHS:
        probe(p, BASE + p + "?" + qs)

    print()
    print("=" * 78)
    print("2. hybrid/daily_profits + 地址参数矩阵")
    print("=" * 78)
    path = "/pf/api/v1/wallets/hybrid/daily_profits"
    best = None
    for key in ADDRESS_KEYS:
        for extra in ({}, {"chain": "sol"}, {"chain": "solana"},
                      {"period": "30d"}, {"type": "1"}):
            q = COMMON_QS.copy()
            q[key] = wallet
            q.update(extra)
            st, body = probe(f"{key} + {extra or '-'}",
                             BASE + path + "?" + urllib.parse.urlencode(q),
                             quiet=False)
            if st == 200:
                try:
                    j = json.loads(body)
                    if j.get("code") == 0:
                        best = (key, extra, body)
                except Exception:
                    pass

    print()
    print("=" * 78)
    print("3. 地址放 path 段: /pf/api/v1/wallets/hybrid/{chain}/{address}/...")
    print("=" * 78)
    for p in [
        "/pf/api/v1/wallets/hybrid/sol/{w}/daily_profits",
        "/pf/api/v1/wallets/hybrid/{w}/daily_profits",
        "/pf/api/v1/wallets/hybrid/daily_profits/{w}",
        "/pf/api/v1/wallets/sol/{w}/daily_profits",
    ]:
        probe(p, BASE + p.format(w=wallet) + "?" + qs)

    print()
    print("=" * 78)
    print("4. 命中样本详情")
    print("=" * 78)
    if best:
        key, extra, body = best
        print(f"命中参数: {key} + {extra}")
        j = json.loads(body)
        data = j.get("data") or {}
        print("data keys:", list(data.keys()))
        lst = None
        for k in ("list", "calendar", "history", "items"):
            if isinstance(data.get(k), list) and data[k]:
                lst = data[k]
                print(f"-> {k}: {len(lst)} 条")
                break
        if lst:
            print("第一条:", json.dumps(lst[0], ensure_ascii=False)[:400])
            print("最后一条:", json.dumps(lst[-1], ensure_ascii=False)[:400])
    else:
        print("没有命中 code=0 的组合")


if __name__ == "__main__":
    main()
