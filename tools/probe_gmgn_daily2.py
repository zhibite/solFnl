#!/usr/bin/env python3
"""第二轮探测: 找出 GMGN daily_profits 的真实可用路径.

手段:
  1. 枚举 /pf/api/v1 与 /defi/quotation/v1 两大前缀下的钱包维度路径
  2. 抓 gmgn.ai 前端 JS bundle, 提取里面的接口常量字符串
  3. 从 bundle 里反推出的候选路径再实测
"""
from __future__ import annotations

import re
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


def fetch_raw(url: str, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            try:
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
            except Exception:
                pass
        return e.code, body.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def summarize(body: str) -> str:
    body = body.strip()
    if not body:
        return "<empty>"
    if body.startswith("<"):
        return "HTML: " + re.sub(r"\s+", " ", body[:120])
    try:
        j = __import__("json").loads(body)
    except Exception:
        return re.sub(r"\s+", " ", body[:180])
    if isinstance(j, dict):
        code = j.get("code", j.get("status", "?"))
        msg = (j.get("message") or j.get("msg") or j.get("reason")
               or j.get("description") or "")
        data = j.get("data")
        extra = ""
        if isinstance(data, dict):
            for key in ("list", "calendar", "rank", "history", "items"):
                lst = data.get(key)
                if isinstance(lst, list) and lst:
                    extra = f" {key}={len(lst)}"
                    break
            if not extra:
                extra = " keys=" + ",".join(list(data.keys())[:8])
        elif isinstance(data, list):
            extra = f" data_list={len(data)}"
        return f"code={code} msg={str(msg)[:70]!r}{extra}"
    return re.sub(r"\s+", " ", body[:150])


def probe(label, url, headers=None, quiet=False):
    status, body = fetch_raw(url, headers)
    if status == 200 and not body.lstrip().startswith("<"):
        print(f"OK [{status}] {label}\n     {summarize(body)}")
        if not quiet:
            print(f"     url: {url[:170]}")
        return status, body
    if not quiet:
        print(f"   [{status:>3}] {label} -> {summarize(body)[:110]}")
    return status, body


def part1_enumerate(wallet: str) -> None:
    print("=" * 78)
    print("1. 路径枚举")
    print("=" * 78)
    qs = urllib.parse.urlencode(COMMON_QS)
    prefixes = [
        "/pf/api/v1/wallets/sol",
        "/defi/quotation/v1/wallets/sol",
        "/pf/api/v1/wallet/sol",
        "/defi/quotation/v1/wallet/sol",
        "/api/v1/wallets/sol",
        "/defi/quotation/v1/rank/sol/wallets",
    ]
    suffixes = [
        "/{w}", "/{w}/pnl", "/{w}/stats", "/{w}/daily_profits",
        "/{w}/pnl_history", "/{w}/profit", "/{w}/trades",
        "/{w}/daily", "/{w}/history", "/{w}/overview",
        "/daily_profits?address={w}",
    ]
    tried = 0
    for pfx in prefixes:
        for sfx in suffixes:
            url = BASE + pfx + sfx.format(w=wallet) + "?" + qs
            st, body = probe("", url, quiet=True)
            tried += 1
            if st == 200 and not body.lstrip().startswith("<"):
                print(f"OK [{st}] {pfx}{sfx}")
                print(f"     {summarize(body)}")
    print(f"(枚举 {tried} 条路径)")


def part2_bundle() -> list[str]:
    print()
    print("=" * 78)
    print("2. 抓前端 JS bundle 找接口常量")
    print("=" * 78)
    st, html = fetch_raw(BASE + "/", headers={
        **HEADERS, "Accept": "text/html,application/xhtml+xml"})
    print(f"首页: [{st}] len={len(html)}")
    scripts = set(re.findall(r'<script[^>]+src="([^"]+)"', html))
    print(f"发现 {len(scripts)} 个 script 标签")
    api_paths: set[str] = set()
    for s in sorted(scripts):
        if not s.startswith("http"):
            s = BASE + s if s.startswith("/") else BASE + "/" + s
        st2, js = fetch_raw(s, headers={**HEADERS, "Accept": "*/*"})
        if st2 != 200:
            continue
        # 抓 /pf/api/... 与 /defi/... 形式的路径字面量
        for m in re.findall(r'["\'](/(?:pf|defi|api)/[A-Za-z0-9_/.\-{}$]+)["\']',
                            js):
            api_paths.add(m)
        for m in re.findall(r'["\']([a-z0-9_]*(?:daily_profit|daily_pnl|pnl_history'
                            r'|profit_history|realized_profit|profit_chart)[a-z0-9_]*)["\']',
                            js):
            api_paths.add(m)
    print(f"bundle 中提取到 {len(api_paths)} 个候选")
    for p in sorted(api_paths):
        print(f"   {p}")
    return sorted(api_paths)


def part3_verify(paths: list[str], wallet: str) -> None:
    print()
    print("=" * 78)
    print("3. 反推路径实测")
    print("=" * 78)
    qs = urllib.parse.urlencode(COMMON_QS)
    # 先只测路径本身能不能命中 (不塞地址)
    for p in paths:
        if p.startswith("/"):
            url = BASE + p + "?" + qs
        else:
            url = BASE + "/pf/api/v1/wallets/sol/" + p + "?" + qs
        st, body = probe(p, url, quiet=True)
        if st == 200 and not body.lstrip().startswith("<"):
            print(f"OK [{st}] {p}\n     {summarize(body)}")
            # 带地址再试
            for tmpl in (p.replace("{w}", wallet),
                         p.rstrip("/") + "/" + wallet):
                url2 = BASE + tmpl + "?" + qs
                st2, b2 = probe("", url2, quiet=True)
                if st2 == 200 and not b2.lstrip().startswith("<"):
                    print(f"OK [{st2}] {tmpl}\n     {summarize(b2)}")


def main():
    wallet = sys.argv[1] if len(sys.argv) > 1 else \
        "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
    part1_enumerate(wallet)
    paths = part2_bundle()
    part3_verify(paths, wallet)


if __name__ == "__main__":
    main()
