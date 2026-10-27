#!/usr/bin/env python3
"""第四轮: 定位 GMGN daily_profits 的真实 host / 路径.

步骤:
  1. 抓全部 JS bundle, 提取 hostname 常量 (api 基础域)
  2. 提取 daily_profits / hybrid 出现处的上下文 (前后 300 字符), 看请求怎么拼
  3. 测试 bundle 里其他 /pf/api/v1 路径, 判断 /pf 前缀整体是否还有效
  4. 在候选 host 上重试 daily_profits
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
    "Accept-Encoding": "gzip, deflate",
}


def fetch(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
            return resp.status, body.decode("utf-8", "replace")
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
        return body[:140].replace("\n", " ")
    if isinstance(j, dict):
        code = j.get("code", j.get("status", "?"))
        msg = (j.get("message") or j.get("msg") or j.get("reason") or "")
        data = j.get("data")
        extra = ""
        if isinstance(data, dict):
            extra = " keys=" + ",".join(list(data.keys())[:8])
        elif isinstance(data, list):
            extra = f" list={len(data)}"
        return f"code={code} msg={str(msg)[:50]!r}{extra}"
    return body[:120]


def collect_bundles():
    st, html = fetch(BASE + "/", {**HEADERS, "Accept": "text/html"})
    srcs = set(re.findall(r'<script[^>]+src="([^"]+)"', html))
    srcs |= set(re.findall(r'["\'](/_next/static/[^"\']+\.js)["\']', html))
    bundles = {}
    for s in srcs:
        url = s if s.startswith("http") else BASE + (s if s.startswith("/") else "/" + s)
        st2, js = fetch(url, {**HEADERS, "Accept": "*/*"})
        if st2 == 200 and len(js) > 500:
            bundles[url] = js
    return bundles


def part_hosts(bundles):
    print("=" * 78)
    print("1. bundle 里的 host 常量")
    print("=" * 78)
    hosts = {}
    for url, js in bundles.items():
        for m in re.findall(r'https?://[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?:/[A-Za-z0-9_/.\-]*)?', js):
            hosts[m] = hosts.get(m, 0) + 1
    for h, c in sorted(hosts.items(), key=lambda x: -x[1])[:40]:
        print(f"  {c:>4}x  {h}")


def part_context(bundles, keyword="daily_profits"):
    print()
    print("=" * 78)
    print(f"2. '{keyword}' 出现处的上下文")
    print("=" * 78)
    for url, js in bundles.items():
        for m in re.finditer(re.escape(keyword), js):
            a = max(0, m.start() - 400)
            b = min(len(js), m.end() + 400)
            ctx = js[a:b].replace("\n", " ")
            print(f"--- {url.split('/')[-1]} @ {m.start()} ---")
            print(ctx)
            print()


def part_pf_paths():
    print("=" * 78)
    print("3. bundle 里其它 /pf/api/v1 路径是否还能用 (判断前缀死活)")
    print("=" * 78)
    qs = urllib.parse.urlencode(COMMON_QS)
    for p in [
        "/pf/api/v1/global_championship",
        "/pf/api/v1/fomo/thesis/token",
        "/pf/api/v1/pump/callout/token",
        "/pf/api/v1/wallets/hybrid/daily_profits",
        "/pf/api/v1/wallets/hybrid/daily_profits/",
        "/pf/api/v1/wallets/hybrid/refresh_balance",
        "/pf/api/v1/wallets/hybrid/person_wallets_pnl_info",
        "/pf/api/v1/wallets/hybrid/wallet_holding_info",
    ]:
        st, body = fetch(BASE + p + "?" + qs)
        print(f"  [{st:>3}] {p} -> {brief(body)}")


def part_hosts_try(bundles):
    print()
    print("=" * 78)
    print("4. 在候选 host 上重试 daily_profits")
    print("=" * 78)
    qs = urllib.parse.urlencode(COMMON_QS)
    wallet = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
    hosts = set()
    for url, js in bundles.items():
        for m in re.findall(r'https?://[a-zA-Z0-9\-]+\.[a-zA-Z0-9.\-]+', js):
            if "gmgn" in m.lower():
                hosts.add(m)
    hosts |= {"https://api.gmgn.ai", "https://pf.gmgn.ai",
              "https://pf-api.gmgn.ai", "https://gmgn.ai"}
    paths = ["/pf/api/v1/wallets/hybrid/daily_profits",
             "/api/v1/wallets/hybrid/daily_profits",
             "/pf/api/v1/wallets/sol/daily_profits",
             "/api/v1/wallets/daily_performance/stats"]
    for h in sorted(hosts):
        for p in paths:
            q = COMMON_QS.copy()
            q["address"] = wallet
            url = h.rstrip("/") + p + "?" + urllib.parse.urlencode(q)
            try:
                st, body = fetch(url, timeout=8)
            except Exception as e:  # noqa: BLE001
                print(f"  [ERR] {url[:110]} {e}")
                continue
            if st == 200 and not body.lstrip().startswith("<"):
                print(f"  OK [{st}] {url[:130]}\n       {brief(body)}")


def main():
    bundles = collect_bundles()
    print(f"抓到 {len(bundles)} 个 bundle\n")
    part_hosts(bundles)
    part_context(bundles, "daily_profits")
    part_pf_paths()
    part_hosts_try(bundles)


if __name__ == "__main__":
    main()
