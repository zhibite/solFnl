#!/usr/bin/env python3
"""Probe GMGN wallet endpoints (fees / tracked / remarks / 7d stats).

Usage:
    python3 tools/probe_gmgn_wallet.py <solana_address>
    python3 tools/probe_gmgn_wallet.py <solana_address> --period 7d --json

Optional auth:
    GMGN_TOKEN=<bearer token> python3 tools/probe_gmgn_wallet.py <address>

Endpoints probed:
    GET  /api/v1/wallet_common_stat/{chain}/{wallet}
         -> follow_count (被追踪), remark_count (被备注), profile info
    GET  /api/v1/wallet_stat/{chain}/{wallet}/{period}
         -> legacy wallet stats; has gas_cost, but often zero for SOL
    GET  /defi/quotation/v1/smartmoney/{chain}/wallet/{wallet}
         -> public 7d realized profit / buy / sell / balance / risk
    GET  /pf/api/v1/wallet/{chain}/{wallet}/profit_stat/{period}
         -> full wallet PnL card incl. total_fee/total_fee_usd/gas_cost,
            requires an Authorization bearer token (GMGN login)
    POST /pf/api/v1/wallets/{chain}/daily_profits
         -> daily profit calendar (same data the crawler uses)

The GMGN access token is a browser login token. It is not printed by this
script; pass it through the GMGN_TOKEN environment variable if you have one.
"""

import argparse
import json
import os
import urllib.error
import urllib.request
from calendar import monthrange
from datetime import datetime, timezone

BASE = "https://gmgn.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _headers(wallet: str, token: str = "") -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://gmgn.ai/sol/address/{wallet}",
        "Origin": "https://gmgn.ai",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _request(method: str, path: str, wallet: str, token: str = "",
             body: dict | None = None, timeout: int = 20):
    url = BASE + path
    data = None
    headers = _headers(wallet, token)
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}"}
    try:
        payload = json.loads(raw)
    except Exception:
        text = raw[:200].decode("utf-8", "replace")
        return {"ok": False, "status": status, "error": f"non-JSON: {text}"}
    return {"ok": status == 200, "status": status, "payload": payload}


def _data(result: dict) -> dict:
    payload = result.get("payload") or {}
    return payload.get("data") or {}


def _pick(data: dict, keys) -> dict:
    return {k: data.get(k) for k in keys if k in data}


def _fee_keys(data: dict) -> dict:
    return {k: v for k, v in data.items()
            if any(x in k.lower() for x in ("fee", "gas", "tip"))}


def _daily(chain: str, wallet: str, month: str, token: str = "") -> dict:
    y, m = map(int, month.split("-"))
    start = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(y, m, monthrange(y, m)[1], 23, 59, 59,
                       tzinfo=timezone.utc).timestamp())
    r = _request("POST", f"/pf/api/v1/wallets/{chain}/daily_profits",
                 wallet, token,
                 {"wallet_addresses": [wallet], "start_at": start,
                  "end_at": end})
    days = _data(r).get("list") or []
    return {
        "status": r["status"], "ok": r["ok"], "days": len(days),
        "sum_profit": round(sum(float(x.get("total_profit") or 0)
                                for x in days), 2),
        "total_buys": sum(int(x.get("total_buys") or 0) for x in days),
        "total_sells": sum(int(x.get("total_sells") or 0) for x in days),
        "first": days[0]["date"] if days else None,
        "last": days[-1]["date"] if days else None,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("wallet")
    p.add_argument("--chain", default="sol")
    p.add_argument("--period", default="7d",
                   choices=["1d", "7d", "30d", "all"])
    p.add_argument("--month", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--token", default=os.getenv("GMGN_TOKEN", ""),
                   help="GMGN bearer token (or set GMGN_TOKEN)")
    args = p.parse_args()

    w = args.wallet.strip()
    chain = "sol" if args.chain.lower() in ("sol", "solana") else args.chain
    period = args.period

    common = _request("GET",
                      f"/api/v1/wallet_common_stat/{chain}/{w}",
                      w, args.token)
    legacy = _request("GET",
                      f"/api/v1/wallet_stat/{chain}/{w}/{period}",
                      w, args.token)
    smart = _request("GET",
                     f"/defi/quotation/v1/smartmoney/{chain}/wallet/{w}",
                     w, args.token)
    profit = _request("GET",
                      f"/pf/api/v1/wallet/{chain}/{w}/profit_stat/{period}",
                      w, args.token)
    calendar = _daily(chain, w, args.month, args.token)

    report = {
        "wallet": w,
        "chain": chain,
        "period": period,
        "common_stat": {
            "status": common["status"],
            "ok": common["ok"],
            **_pick(_data(common), (
                "follow_count", "remark_count", "followers_count",
                "tags", "tag_rank", "created_at", "fund_from_address",
            )),
        },
        "legacy_wallet_stat": {
            "status": legacy["status"],
            "ok": legacy["ok"],
            **_pick(_data(legacy), (
                "buy", "sell", "buy_7d", "sell_7d",
                "pnl", "pnl_7d", "realized_profit_7d",
                "gas_cost", "follow_count", "remark_count",
            )),
            "fee_like_fields": _fee_keys(_data(legacy)),
        },
        "smartmoney_wallet": {
            "status": smart["status"],
            "ok": smart["ok"],
            **_pick(_data(smart), (
                "balance", "total_profit", "total_profit_pnl",
                "realized_profit", "realized_profit_7d", "pnl_7d",
                "buy_7d", "sell_7d", "risk",
            )),
        },
        "profit_stat_auth": {
            "status": profit["status"],
            "ok": profit["ok"],
            "message": (profit.get("payload") or {}).get("message"),
            **_pick(_data(profit), (
                "total_fee", "total_fee_usd", "gas_cost",
                "avg_holding_peroid", "history_bought_cost",
                "total_volume", "buy", "sell", "total_profit",
                "total_profit_pnl", "realized_profit",
            )),
            "fee_like_fields": _fee_keys(_data(profit)),
        },
        "daily_profits": calendar,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"wallet: {w}  chain={chain} period={period}")
    print(f"common_stat [{report['common_stat']['status']}]: "
          f"follow={report['common_stat'].get('follow_count')} "
          f"remark={report['common_stat'].get('remark_count')} "
          f"tags={report['common_stat'].get('tags')}")
    print(f"legacy_wallet_stat [{report['legacy_wallet_stat']['status']}]: "
          f"{report['legacy_wallet_stat']}")
    print(f"smartmoney [{report['smartmoney_wallet']['status']}]: "
          f"7d_profit={report['smartmoney_wallet'].get('realized_profit_7d')} "
          f"7d_buy/sell={report['smartmoney_wallet'].get('buy_7d')}/"
          f"{report['smartmoney_wallet'].get('sell_7d')}")
    print(f"profit_stat [{report['profit_stat_auth']['status']}]: "
          f"{report['profit_stat_auth'].get('message') or ''} "
          f"{report['profit_stat_auth']['fee_like_fields']}")
    print(f"daily_profits [{calendar['status']}]: days={calendar['days']} "
          f"sum_profit={calendar['sum_profit']} "
          f"buys/sells={calendar['total_buys']}/{calendar['total_sells']}")
    print("\nSet GMGN_TOKEN (browser login token) to unlock profit_stat "
          "(total_fee/gas_cost/holding time).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
