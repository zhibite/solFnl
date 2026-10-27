"""Full-flow check: seed DB from docs/debot.md sample, query pages + public APIs."""
import json

from fastapi.testclient import TestClient

import app as m
from database import get_db

# 1. parse docs/debot.md (line1 = url, rest = JSON)
raw = open(r"D:\05.project2\3.solana\gmgndaily\docs\debot.md", encoding="utf-8").read()
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

# 3. pages + public APIs via TestClient
c = TestClient(m.app)
print("home:", c.get("/?month=2026-09").status_code)
print("wallets-page:", c.get("/wallets").status_code)
print("detail:", c.get(f"/wallet/{wallet}?month=2026-09").status_code)
r = c.get(f"/api/profit/{wallet}?month=2026-09").json()
print("api single: success=", r["success"], "days=", len(r["days"]),
      "month-pnl=", round(r["summary"]["realized_profit"], 2))
r = c.get("/api/profit?month=2026-09").json()
print("api all: success=", r["success"], "total=", round(r["total_pnl"], 2))
print("ALL_OK")
