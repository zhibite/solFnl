"""SQLite persistence for gmgndaily.

Tables:
  wallets      - tracked sol addresses (address PK, label, enabled, months config)
  daily_profit - one row per (wallet, date); fields mirror docs/debot.md calendar items
  sync_log     - history of sync runs per (wallet, month)
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = "/data/gmgndaily.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Optional[str] = None):
        import os
        self.db_path = db_path or os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise PermissionError(
                f"cannot create database directory "
                f"{Path(self.db_path).parent}: {e}. "
                f"Check the /data volume mount ownership."
            ) from e
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    address TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # Track which source was used for each record (for debugging/audit)
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_profit_source (
                    wallet TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'debot',
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (wallet, date),
                    FOREIGN KEY (wallet, date) REFERENCES daily_profit(wallet, date)
                        ON DELETE CASCADE
                )
            """)
            # One row per wallet per calendar day. Column names mirror
            # docs/debot.md calendar items 1:1.
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_profit (
                    wallet TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    buy_count INTEGER NOT NULL DEFAULT 0,
                    sell_count INTEGER NOT NULL DEFAULT 0,
                    buy_volume REAL NOT NULL DEFAULT 0,
                    sell_volume REAL NOT NULL DEFAULT 0,
                    realized_profit REAL NOT NULL DEFAULT 0,
                    gains REAL NOT NULL DEFAULT 0,
                    losses REAL NOT NULL DEFAULT 0,
                    last_trade_time INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (wallet, date)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet TEXT NOT NULL,
                    month TEXT NOT NULL,
                    days INTEGER NOT NULL DEFAULT 0,
                    month_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ok',
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            # key-value config (debot session, sync interval, ...)
            c.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_daily_wallet_date "
                      "ON daily_profit (wallet, date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sync_wallet_month "
                      "ON sync_log (wallet, month)")

    # ============ wallets ============

    def upsert_wallet(self, address: str, label: str = "",
                      enabled: bool = True) -> None:
        address = address.strip()
        if not address:
            raise ValueError("address is empty")
        now = _now_iso()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO wallets (address, label, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    label=excluded.label, enabled=excluded.enabled,
                    updated_at=excluded.updated_at
            """, (address, label, 1 if enabled else 0, now, now))

    def import_wallets(self, items: List[Dict[str, Any]]) -> Dict[str, int]:
        """Bulk import from JSON list: [{address, label?, enabled?}]."""
        added = skipped = 0
        for it in items:
            addr = str(it.get("address", "")).strip()
            if not addr:
                skipped += 1
                continue
            self.upsert_wallet(addr,
                               label=str(it.get("label", "")),
                               enabled=bool(it.get("enabled", True)))
            added += 1
        return {"added": added, "skipped": skipped}

    def get_wallet(self, address: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM wallets WHERE address=?",
                               (address,)).fetchone()
            return dict(row) if row else None

    def list_wallets(self, only_enabled: bool = False) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if only_enabled:
                rows = conn.execute(
                    "SELECT * FROM wallets WHERE enabled=1 ORDER BY created_at").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wallets ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def delete_wallet(self, address: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM wallets WHERE address=?", (address,))
            conn.execute("DELETE FROM daily_profit WHERE wallet=?", (address,))
            return cur.rowcount

    def set_wallet_enabled(self, address: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE wallets SET enabled=?, updated_at=? WHERE address=?",
                         (1 if enabled else 0, _now_iso(), address))

    # ============ daily_profit ============

    def upsert_days(self, wallet: str, days: List[Dict[str, Any]],
                    source: str = "debot") -> int:
        """Upsert calendar days for a wallet. Returns rows written.
        
        source: which data source this came from (debot/gmgn)
        """
        now = _now_iso()
        n = 0
        with self._conn() as conn:
            for d in days:
                conn.execute("""
                    INSERT INTO daily_profit
                        (wallet, date, buy_count, sell_count, buy_volume,
                         sell_volume, realized_profit, gains, losses,
                         last_trade_time, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet, date) DO UPDATE SET
                        buy_count=excluded.buy_count,
                        sell_count=excluded.sell_count,
                        buy_volume=excluded.buy_volume,
                        sell_volume=excluded.sell_volume,
                        realized_profit=excluded.realized_profit,
                        gains=excluded.gains,
                        losses=excluded.losses,
                        last_trade_time=excluded.last_trade_time,
                        synced_at=excluded.synced_at
                """, (wallet, d["date"], d["buy_count"], d["sell_count"],
                      d["buy_volume"], d["sell_volume"], d["realized_profit"],
                      d["gains"], d["losses"], d["last_trade_time"], now))
                
                # Track which source this record came from
                conn.execute("""
                    INSERT INTO daily_profit_source (wallet, date, source, synced_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(wallet, date) DO UPDATE SET
                        source=excluded.source, synced_at=excluded.synced_at
                """, (wallet, d["date"], source, now))
                n += 1
        return n

    def get_daily(self, wallet: str, month: Optional[str] = None,
                  limit: int = 500) -> List[Dict[str, Any]]:
        """Get daily rows for a wallet, optionally filtered by month 'YYYY-MM'."""
        with self._conn() as conn:
            if month:
                y, m = month.split("-")
                import calendar as _cal
                import datetime as _dt
                start = int(_dt.datetime(int(y), int(m), 1,
                                         tzinfo=timezone.utc).timestamp())
                last = _cal.monthrange(int(y), int(m))[1]
                end = int(_dt.datetime(int(y), int(m), last, 23, 59, 59,
                                       tzinfo=timezone.utc).timestamp())
                rows = conn.execute("""
                    SELECT * FROM daily_profit
                    WHERE wallet=? AND date>=? AND date<=? ORDER BY date
                """, (wallet, start, end)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM daily_profit
                    WHERE wallet=? ORDER BY date DESC LIMIT ?
                """, (wallet, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_month_summary(self, wallet: str, month: str) -> Dict[str, Any]:
        days = self.get_daily(wallet, month)
        return {
            "wallet": wallet,
            "month": month,
            "days": len(days),
            "realized_profit": sum(d["realized_profit"] for d in days),
            "gains": sum(d["gains"] for d in days),
            "losses": sum(d["losses"] for d in days),
            "buy_count": sum(d["buy_count"] for d in days),
            "sell_count": sum(d["sell_count"] for d in days),
        }

    def get_all_summaries(self, month: str) -> List[Dict[str, Any]]:
        wallets = self.list_wallets(only_enabled=True)
        return [self.get_month_summary(w["address"], month) for w in wallets]

    # ============ sync_log ============

    def add_sync_log(self, wallet: str, month: str, days: int,
                     month_pnl: float, status: str = "ok",
                     message: str = "") -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO sync_log
                    (wallet, month, days, month_pnl, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (wallet, month, days, month_pnl, status, message, _now_iso()))

    def get_sync_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ============ config kv ============

    def get_config(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row["value"])
            return default

    def set_config(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?
            """, (key, json.dumps(value), _now_iso(), json.dumps(value), _now_iso()))


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
