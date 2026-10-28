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

        r = (await c.get("/api/profit?month=2026-09")).json()
        print("api all: success=", r["success"],
              "total=", round(r["total_pnl"], 2))

        # 4. sanity: unknown page params must not 500
        print("bad page:", (await c.get("/?page=abc")).status_code)
        print("bad month:", (await c.get(
            f"/profit/{wallet}?month=abc")).status_code)
        print("ALL_OK")


asyncio.run(main())
