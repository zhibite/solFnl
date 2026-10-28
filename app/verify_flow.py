"""Full-flow check: seed DB from docs/debot.md sample, query pages + public APIs.

Run from anywhere:  python app/verify_flow.py
Requirements: `pip install -r requirements-dev.txt` (needs httpx).
Uses a throw-away SQLite file, never the production DB.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

# Must be set before importing app/database (config is read at import time).
_tmp_db = Path(tempfile.gettempdir()) / "gmgndaily_verify.db"
_tmp_db.unlink(missing_ok=True)
os.environ["DATABASE_PATH"] = str(_tmp_db)
os.environ["GMGN_PASSWORD"] = "verify-only"

import httpx  # noqa: E402

import app as m  # noqa: E402
from database import get_db  # noqa: E402

# 1. parse docs/debot.md (line 1 = url, rest = JSON)
raw = (Path(__file__).resolve().parent.parent / "docs" / "debot.md").read_text(
    encoding="utf-8")
payload = json.loads("\n".join(raw.splitlines()[1:]))
days = payload["data"]["calendar"]
print("sample days:", len(days))

# 2. seed wallets + days directly
db = get_db()
wallet = "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP"
db.upsert_wallet(wallet, label="test1")
n = db.upsert_days(wallet, days)
print("upserted:", n)
print("summary:", db.get_month_summary(wallet, "2026-09"))

# 2b. GMGN-shaped day (all fields, incl. transfers) through normalize()
from gmgn_client import normalize  # noqa: E402

gmgn_wallet = "Ar2Y6o1QmrRAskjii1cRfijeKugHH13ycxW5cd7rro1x"
gmgn_day = normalize({
    "date": 1790812800,
    "total_profit": "27686.01876755388696498647",
    "total_buys": 3192,
    "total_sells": 3186,
    "total_transfer_ins": 516,
    "total_transfer_outs": 0,
    "buy_amount_usd": "878707.88955879398290654682",
    "sell_amount_usd": "908563.35947402332295534482",
    "transfer_in_amount_usd": "31915.25194852506356579778",
    "transfer_out_amount_usd": "0",
    "win_sells": 1601,
    "loss_sells": 1585,
    "win_profit": "82295.95320490874054763786",
    "loss_profit": "-54609.93443735485358265139",
})
db.upsert_wallet(gmgn_wallet, label="gmgn-sample")
db.upsert_days(gmgn_wallet, [gmgn_day], source="gmgn")
print("gmgn sample seeded, transfer_in_amount_usd:",
      gmgn_day["transfer_in_amount_usd"])


async def main():
    # 3. pages + public APIs (httpx ASGITransport; TestClient breaks on
    #    httpx>=0.28 + old starlette)
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        r = await c.post("/login",
                         data={"username": "zengsw",
                               "password": "verify-only"},
                         follow_redirects=False)
        print("login:", r.status_code)
        print("home:", (await c.get("/?month=2026-09")).status_code)
        print("wallets-page:", (await c.get("/wallets")).status_code)
        print("detail:", (await c.get(
            f"/wallet/{wallet}?month=2026-09")).status_code)

        r = (await c.get(f"/api/profit/{wallet}?month=2026-09")).json()
        print("api single: success=", r["success"], "status=", r.get("status"),
              "days=", len(r["days"]),
              "month-pnl=", round(r["summary"]["realized_profit"], 2))

        r = (await c.get(f"/api/profit/{gmgn_wallet}?month=2026-10")).json()
        d0 = r["days"][0]
        assert d0["transfer_in_count"] == 516
        assert abs(d0["transfer_in_amount_usd"] - 31915.2519485) < 1e-4
        print("api gmgn day: transfer_in_count=", d0["transfer_in_count"],
              "transfer_in_amount_usd=", round(d0["transfer_in_amount_usd"], 2))

        r = (await c.get("/api/profit?month=2026-09")).json()
        print("api all: success=", r["success"],
              "total=", round(r["total_pnl"], 2))

        r = (await c.get("/api/profit?month=2026-10")).json()
        row = next(w for w in r["wallets"] if w["wallet"] == gmgn_wallet)
        assert row["transfer_in_count"] == 516
        assert row["buy_volume"] > 0
        print("api list aggregates: transfer_in_count=",
              row["transfer_in_count"],
              "buy_volume=", round(row["buy_volume"], 2))

        # 4. sanity: unknown page params must not 500
        print("bad page:", (await c.get("/?page=abc")).status_code)
        print("bad month:", (await c.get(
            f"/profit/{wallet}?month=abc")).status_code)
        print("ALL_OK")


asyncio.run(main())
