#!/usr/bin/env python3
"""第六轮: 完整验证 GMGN daily_profits (POST).

已确认:
  POST /pf/api/v1/wallets/sol/daily_profits
  body: {"wallet_addresses": ["<addr>"], "start_at": <ts>, "end_at": <ts>}
  -> code=0, data.list[]

本轮验证:
  1. 完整响应体样本 (字段名 vs docs/gmgn-daily.md)
  2. 与 docs/gmgn-daily.md 样本逐字段核对
  3. 多钱包批量
  4. 参数边界 (start/end 必填 / 1 年上限 / limit / 单日)
  5. 是否需要鉴权
  6. 连续请求稳定性
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://gmgn.ai"
PATH = "/pf/api/v1/wallets/sol/daily_profits"
WALLET = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
WALLET2 = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"

MIN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://gmgn.ai/",
    "Origin": "https://gmgn.ai",
}


def post(body, headers=None, path=PATH, qs=None):
    url = BASE + path
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers=headers or MIN_HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def ts(dt_str):
    return int(datetime.strptime(dt_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def dstr(tsval):
    return datetime.fromtimestamp(tsval, timezone.utc).strftime("%Y-%m-%d")


def show(st, body, label):
    print(f"[{st}] {label}")
    try:
        j = json.loads(body)
    except Exception:
        print("   ", body[:200])
        return None
    print(f"    code={j.get('code')} msg={j.get('message') or j.get('reason')}")
    return j


def main():
    print("=" * 78)
    print("1. 最小请求 (无 token / 无公共 query)")
    print("=" * 78)
    body = {"wallet_addresses": [WALLET],
            "start_at": ts("2026-08-01"), "end_at": ts("2026-10-01")}
    st, resp = post(body)
    j = show(st, resp, "单钱包 2026-08-01..2026-10-01")
    sample = None
    if j and j.get("code") == 0:
        lst = j["data"]["list"]
        print(f"    list 条数 = {len(lst)}")
        print(f"    date {lst[0]['date']} ({dstr(lst[0]['date'])}) .. "
              f"{lst[-1]['date']} ({dstr(lst[-1]['date'])})")
        print(f"    字段名 = {list(lst[0].keys())}")
        print("    首条:")
        print("      " + json.dumps(lst[0], ensure_ascii=False, indent=2)
              .replace("\n", "\n      "))
        sample = lst

    print()
    print("=" * 78)
    print("2. 与 docs/gmgn-daily.md 样本核对")
    print("=" * 78)
    try:
        raw = open("docs/gmgn-daily.md", encoding="utf-8-sig").read()
        doc = json.loads(raw[raw.index("{"):])
        doc_list = doc["data"]["list"]
        print(f"    文档样本 {len(doc_list)} 条, "
              f"date {doc_list[0]['date']} ({dstr(doc_list[0]['date'])}) .. "
              f"{doc_list[-1]['date']} ({dstr(doc_list[-1]['date'])})")
        doc_keys = set(doc_list[0].keys())
        if sample:
            api_keys = set(sample[0].keys())
            print(f"    文档有接口无: {sorted(doc_keys - api_keys)}")
            print(f"    接口有文档无: {sorted(api_keys - doc_keys)}")
            doc_by_date = {d["date"]: d for d in doc_list}
            api_by_date = {d["date"]: d for d in sample}
            common = sorted(set(doc_by_date) & set(api_by_date))
            print(f"    date 交集 = {len(common)} 天")
            if common:
                for d0 in common[:2]:
                    print(f"    --- 对比 {d0} ({dstr(d0)}) ---")
                    for k in sorted(doc_by_date[d0].keys()):
                        a, b = doc_by_date[d0].get(k), api_by_date[d0].get(k)
                        same = str(a) == str(b)
                        print(f"      {k:<22} doc={str(a)[:26]:<28} "
                              f"api={str(b)[:26]:<28} {'==' if same else '!='}")
                # 汇总对比
                doc_sum = sum(float(x["total_profit"]) for x in doc_list
                              if x["date"] in set(common))
                api_sum = sum(float(x["total_profit"]) for x in sample
                              if x["date"] in set(common))
                print(f"    交集区间 total_profit 合计: "
                      f"doc={doc_sum:.4f} api={api_sum:.4f} "
                      f"差={api_sum - doc_sum:.4f}")
            else:
                print("    !! 无交集日期 — 文档样本可能是另一个钱包")
    except FileNotFoundError:
        print("    (没有 docs/gmgn-daily.md, 跳过)")

    print()
    print("=" * 78)
    print("3. 多钱包批量")
    print("=" * 78)
    st, resp = post({"wallet_addresses": [WALLET, WALLET2],
                     "start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")})
    j = show(st, resp, "两个钱包")
    if j and j.get("code") == 0:
        data = j["data"]
        print(f"    data keys = {list(data.keys())}")
        lst = data.get("list") or []
        addrs = {d.get("address") or d.get("wallet_address") for d in lst}
        print(f"    list 条数 = {len(lst)}")
        print(f"    涉及地址 = {addrs}")
        for k, v in data.items():
            if k != "list":
                print(f"    data[{k}] = {json.dumps(v, ensure_ascii=False)[:200]}")

    print()
    print("=" * 78)
    print("4. 参数边界")
    print("=" * 78)
    cases = [
        ("不给 start/end",
         {"wallet_addresses": [WALLET]}),
        ("只给 start_at",
         {"wallet_addresses": [WALLET], "start_at": ts("2026-09-01")}),
        ("只给 end_at",
         {"wallet_addresses": [WALLET], "end_at": ts("2026-10-01")}),
        ("单日",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2026-09-15"), "end_at": ts("2026-09-16")}),
        ("整月 2026-09",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")}),
        ("跨度 2 年",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2025-01-01"), "end_at": ts("2026-10-01")}),
        ("跨度 350 天",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2025-10-15"), "end_at": ts("2026-10-01")}),
        ("带 limit=10",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2026-08-01"), "end_at": ts("2026-10-01"),
          "limit": 10}),
        ("未来区间",
         {"wallet_addresses": [WALLET],
          "start_at": ts("2026-11-01"), "end_at": ts("2026-12-01")}),
        ("不存在的地址",
         {"wallet_addresses": ["11111111111111111111111111111111"],
          "start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")}),
        ("钱包地址传字符串",
         {"wallet_addresses": WALLET,
          "start_at": ts("2026-09-01"), "end_at": ts("2026-10-01")}),
    ]
    for label, b in cases:
        st, resp = post(b)
        try:
            j = json.loads(resp)
            n = ""
            if j.get("code") == 0:
                lst = (j.get("data") or {}).get("list") or []
                n = f" list={len(lst)}"
                if lst and isinstance(lst[0], dict) and lst[0].get("date"):
                    n += f" {dstr(lst[0]['date'])}..{dstr(lst[-1]['date'])}"
            print(f"  [{st:>3}] {label:<18} code={j.get('code')} "
                  f"msg={(j.get('message') or j.get('reason') or '')[:50]!r}{n}")
        except Exception:
            print(f"  [{st:>3}] {label:<18} {resp[:110]}")

    print()
    print("=" * 78)
    print("5. 鉴权 / 公共 query 是否必要")
    print("=" * 78)
    extra_headers = dict(MIN_HEADERS)
    extra_headers["Authorization"] = "Bearer dummy"
    st, resp = post(body, headers=extra_headers)
    j = show(st, resp, "带假 Authorization")
    if j and j.get("code") == 0:
        print(f"    list={len(j['data']['list'])} -> 无需鉴权")

    qs = {"device_id": "89546a76-a88d-4e75-9b81-af3aa735fb3a",
          "client_id": "gmgn_web_20261006-5111-3972763"}
    st, resp = post(body, qs=qs)
    j = show(st, resp, "带公共 query")
    if j and j.get("code") == 0:
        print(f"    list={len(j['data']['list'])} -> query 非必需")

    print()
    print("=" * 78)
    print("6. 连续 10 次稳定性")
    print("=" * 78)
    codes = []
    for _ in range(10):
        st, resp = post(body)
        try:
            codes.append((st, json.loads(resp).get("code")))
        except Exception:
            codes.append((st, "?"))
    print(f"    {codes}")


if __name__ == "__main__":
    main()
