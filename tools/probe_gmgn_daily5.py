#!/usr/bin/env python3
"""第五轮: POST 实测 daily_profits / daily_performance/stats.

bundle 源码还原出的三条路径:
  A) POST /pf/api/v1/wallets/hybrid/daily_profits
     body: {wallet_addresses:[...], ...}
  B) POST /pf/api/v1/wallets/{chain}/daily_profits
     body: {wallet_addresses:[...], start_at, end_at}
  C) POST /api/v1/wallets/daily_performance/stats
     body: {chain, wallet_addresses:[...], started_at, ended_at, limit}

同时从 bundle 里找 axios baseURL 与鉴权 header 名。
"""
from __future__ import annotations

import gzip
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

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
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

WALLET = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"


def fetch(url, body=None, headers=None, timeout=25, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers or HEADERS,
        method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def brief(body: str) -> str:
    body = body.strip()
    if not body:
        return "<empty>"
    if body.startswith("<"):
        return "HTML:" + body[:80].replace("\n", " ")
    try:
        j = json.loads(body)
    except Exception:
        return body[:200].replace("\n", " ")
    if not isinstance(j, dict):
        return body[:200]
    code = j.get("code", j.get("status", "?"))
    msg = (j.get("message") or j.get("msg") or j.get("reason")
           or j.get("description") or "")
    data = j.get("data")
    extra = ""
    if isinstance(data, dict):
        extra = " keys=" + ",".join(list(data.keys())[:10])
        for k in ("list", "calendar", "history", "items", "stats", "rank"):
            v = data.get(k)
            if isinstance(v, list) and v:
                extra = f" {k}={len(v)}"
                if isinstance(v[0], dict) and v[0].get("date"):
                    extra += f" {v[0]['date']}..{v[-1].get('date')}"
                break
    elif isinstance(data, list):
        extra = f" list={len(data)}"
    return f"code={code} msg={str(msg)[:70]!r}{extra}"


def post(label, path, body, headers=None, qs=None):
    url = BASE + path
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    st, resp = fetch(url, body=body, headers=headers)
    print(f"[{st:>3}] {label}")
    print(f"      body: {json.dumps(body, ensure_ascii=False)[:160]}")
    print(f"      -> {brief(resp)}")
    return st, resp


def find_baseurl():
    print("=" * 78)
    print("0. bundle 里的 baseURL / 鉴权 header")
    print("=" * 78)
    st, html = fetch(BASE + "/", {**HEADERS, "Accept": "text/html"})
    srcs = set(re.findall(r'<script[^>]+src="([^"]+)"', html))
    for s in sorted(srcs):
        url = s if s.startswith("http") else BASE + "/" + s.lstrip("/")
        st2, js = fetch(url, {**HEADERS, "Accept": "*/*"})
        if st2 != 200 or len(js) < 500:
            continue
        if "daily_profits" not in js:
            continue
        for m in re.finditer(r'baseURL[^,;]{0,160}', js):
            print("  baseURL:", m.group(0)[:160])
        for m in re.finditer(r'"/pf/api[^"]*"', js):
            print("  pfpath :", m.group(0))
        # 鉴权: 找 authType Access 对应的 header/token
        for kw in ["Authorization", "access_token", "x-access-token",
                   "Bearer", "gmgn_access", "session"]:
            for m in re.finditer(r'.{0,90}' + re.escape(kw) + r'.{0,90}', js):
                seg = m.group(0).replace("\n", " ")
                if "header" in seg.lower() or "token" in seg.lower():
                    print(f"  auth[{kw}]:", seg[:180])
                    break
        break


def main():
    print(f"wallet = {WALLET}\n")
    find_baseurl()

    print()
    print("=" * 78)
    print("A. POST /api/v1/wallets/daily_performance/stats")
    print("=" * 78)
    bodies = [
        {"chain": "sol", "wallet_addresses": [WALLET]},
        {"chain": "sol", "wallet_addresses": [WALLET], "limit": 100},
        {"chain": "solana", "wallet_addresses": [WALLET], "limit": 100},
        {"chain": "sol", "wallet_addresses": WALLET},
        {"chain": "sol", "wallet_addresses": [WALLET],
         "started_at": 1785542400, "ended_at": 1790000000, "limit": 100},
    ]
    for b in bodies:
        post(str(b)[:60], "/api/v1/wallets/daily_performance/stats", b)

    print()
    print("=" * 78)
    print("B. POST /pf/api/v1/wallets/sol/daily_profits")
    print("=" * 78)
    for b in [
        {"wallet_addresses": [WALLET]},
        {"wallet_addresses": [WALLET], "start_at": 1785542400,
         "end_at": 1790000000},
        {"wallet_addresses": WALLET},
    ]:
        post(str(b)[:60], "/pf/api/v1/wallets/sol/daily_profits", b)

    print()
    print("=" * 78)
    print("C. POST /pf/api/v1/wallets/hybrid/daily_profits")
    print("=" * 78)
    for b in [
        {"wallet_addresses": [WALLET]},
        {"wallet_addresses": [WALLET], "chain": "sol"},
        {"chain": "sol", "wallet_addresses": [WALLET], "limit": 100},
    ]:
        post(str(b)[:60], "/pf/api/v1/wallets/hybrid/daily_profits", b)

    print()
    print("=" * 78)
    print("D. 其它 chain 写法 (确认路由)")
    print("=" * 78)
    for chain in ["sol", "solana", "bsc", "eth", "base"]:
        post(f"chain={chain}", f"/pf/api/v1/wallets/{chain}/daily_profits",
             {"wallet_addresses": [WALLET]})


if __name__ == "__main__":
    main()
