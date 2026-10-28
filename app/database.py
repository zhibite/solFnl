"""SQLite persistence for gmgndaily.

Tables:
  wallets            - tracked sol addresses (address PK, label, enabled)
  daily_profit       - one row per (wallet, date); mirrors the GMGN daily item
  daily_profit_source- which upstream each (wallet, date) came from
  sync_log           - history of sync runs per (wallet, month), pruned by age
  sync_state         - latest durable sync status per (wallet, month)
  config             - key/value app settings
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from validators import is_valid_sol_address, parse_month

DEFAULT_DB_PATH = "/data/gmgndaily.db"

# Upsert keeps an existing label when the incoming label is empty, and keeps
# the existing enabled flag when `enabled` is not supplied (NULL).
_UPSERT_WALLET_SQL = """
    INSERT INTO wallets (address, label, enabled, created_at, updated_at)
    VALUES (?, ?, COALESCE(?, 1), ?, ?)
    ON CONFLICT(address) DO UPDATE SET
        label = CASE WHEN excluded.label <> '' THEN excluded.label
                     ELSE wallets.label END,
        enabled = COALESCE(?, wallets.enabled),
        updated_at = excluded.updated_at
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _win_rate(win_sells: int, loss_sells: int) -> Optional[float]:
    """胜率 = 盈利单数 / (盈利单数 + 亏损单数). 无卖出单时返回 None."""
    total = (win_sells or 0) + (loss_sells or 0)
    if total <= 0:
        return None
    return (win_sells or 0) / total


def _recent_cutoff(days: int = 7) -> int:
    """近 N 天滚动窗口的起始时间戳 (UTC, 含今天往回推 N*24h)."""
    import datetime as _dt
    now = int(_dt.datetime.now(tz=timezone.utc).timestamp())
    return now - days * 86400


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
        # timeout + busy_timeout let concurrent writer threads wait instead of
        # failing immediately with "database is locked".
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
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
            # WAL: readers don't block the sync writer (and vice versa).
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                # e.g. a network filesystem that cannot do WAL; keep running
                # with the default journal mode.
                pass
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
            # One row per wallet per calendar day. Every field returned by the
            # GMGN daily_profits API has a column here; see
            # gmgn_client.normalize() for the field-by-field mapping.
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_profit (
                    wallet TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    buy_count INTEGER NOT NULL DEFAULT 0,
                    sell_count INTEGER NOT NULL DEFAULT 0,
                    transfer_in_count INTEGER NOT NULL DEFAULT 0,
                    transfer_out_count INTEGER NOT NULL DEFAULT 0,
                    buy_volume REAL NOT NULL DEFAULT 0,
                    sell_volume REAL NOT NULL DEFAULT 0,
                    transfer_in_amount_usd REAL NOT NULL DEFAULT 0,
                    transfer_out_amount_usd REAL NOT NULL DEFAULT 0,
                    realized_profit REAL NOT NULL DEFAULT 0,
                    gains REAL NOT NULL DEFAULT 0,
                    losses REAL NOT NULL DEFAULT 0,
                    win_sells INTEGER NOT NULL DEFAULT 0,
                    loss_sells INTEGER NOT NULL DEFAULT 0,
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
            # Latest sync state per (wallet, month). Durable across sync_log
            # pruning, and used to tell "synced but zero trades" apart from
            # "never synced" in /api/profit.
            c.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    wallet TEXT NOT NULL,
                    month TEXT NOT NULL,
                    days INTEGER NOT NULL DEFAULT 0,
                    month_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ok',
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (wallet, month)
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
            c.execute("CREATE INDEX IF NOT EXISTS idx_daily_date "
                      "ON daily_profit (date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_wallets_created "
                      "ON wallets (created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_wallets_enabled "
                      "ON wallets (enabled)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sync_wallet_month "
                      "ON sync_log (wallet, month)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sync_wallet_created "
                      "ON sync_log (wallet, created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sync_created "
                      "ON sync_log (created_at)")
            # ---- 轻量迁移: 老库缺列时自动补上 ----
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(daily_profit)").fetchall()}
            new_columns = {
                "win_sells": "INTEGER NOT NULL DEFAULT 0",
                "loss_sells": "INTEGER NOT NULL DEFAULT 0",
                "transfer_in_count": "INTEGER NOT NULL DEFAULT 0",
                "transfer_out_count": "INTEGER NOT NULL DEFAULT 0",
                "transfer_in_amount_usd": "REAL NOT NULL DEFAULT 0",
                "transfer_out_amount_usd": "REAL NOT NULL DEFAULT 0",
            }
            for name, decl in new_columns.items():
                if name not in cols:
                    conn.execute(
                        f"ALTER TABLE daily_profit ADD COLUMN {name} {decl}")

    # ============ wallets ============

    def upsert_wallet(self, address: str, label: str = "",
                      enabled: Optional[bool] = None) -> None:
        """Insert or update a wallet.

        label: empty string leaves an existing label untouched.
        enabled: None leaves the existing flag untouched (default 1 on insert).
        """
        address = (address or "").strip()
        if not address:
            raise ValueError("address is empty")
        now = _now_iso()
        enabled_val = None if enabled is None else (1 if enabled else 0)
        with self._conn() as conn:
            conn.execute(_UPSERT_WALLET_SQL,
                         (address, label or "", enabled_val, now, now,
                          enabled_val))

    def import_wallets(self, items: List[Any]) -> Dict[str, int]:
        """Bulk import from JSON list in one transaction.

        Accepts [{address, label?, enabled?}] or ["<address>", ...].
        Invalid Solana addresses are skipped and counted separately.
        """
        added = updated = skipped = invalid = 0
        now = _now_iso()
        with self._conn() as conn:
            existing = {r["address"] for r in
                        conn.execute("SELECT address FROM wallets")}
            for it in items:
                if isinstance(it, str):
                    addr, label, enabled = it.strip(), "", None
                elif isinstance(it, dict):
                    addr = str(it.get("address") or "").strip()
                    label = str(it.get("label") or "")
                    raw_enabled = it.get("enabled")
                    if isinstance(raw_enabled, str):
                        enabled = raw_enabled.strip().lower() not in (
                            "", "0", "false", "no", "off")
                    else:
                        enabled = (None if raw_enabled is None
                                   else bool(raw_enabled))
                else:
                    skipped += 1
                    continue
                if not addr:
                    skipped += 1
                    continue
                if not is_valid_sol_address(addr):
                    invalid += 1
                    continue
                enabled_val = None if enabled is None else (1 if enabled else 0)
                conn.execute(_UPSERT_WALLET_SQL,
                             (addr, label, enabled_val, now, now, enabled_val))
                if addr in existing:
                    updated += 1
                else:
                    added += 1
                    existing.add(addr)
        return {"added": added, "updated": updated,
                "skipped": skipped, "invalid": invalid}

    def get_wallet(self, address: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM wallets WHERE address=?",
                               (address,)).fetchone()
            return dict(row) if row else None

    def list_wallets(self, only_enabled: bool = False,
                     limit: Optional[int] = None,
                     offset: int = 0) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            where = "WHERE enabled=1" if only_enabled else ""
            sql = f"SELECT * FROM wallets {where} ORDER BY created_at"
            params: List[Any] = []
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params += [limit, offset]
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def count_wallets(self, only_enabled: bool = False) -> int:
        with self._conn() as conn:
            where = "WHERE enabled=1" if only_enabled else ""
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM wallets {where}").fetchone()
            return int(row["n"]) if row else 0

    def delete_wallet(self, address: str) -> int:
        """Delete the wallet plus all of its daily/source/sync-log rows."""
        with self._conn() as conn:
            conn.execute("DELETE FROM daily_profit_source WHERE wallet=?",
                         (address,))
            conn.execute("DELETE FROM daily_profit WHERE wallet=?", (address,))
            conn.execute("DELETE FROM sync_log WHERE wallet=?", (address,))
            conn.execute("DELETE FROM sync_state WHERE wallet=?", (address,))
            cur = conn.execute("DELETE FROM wallets WHERE address=?", (address,))
            return cur.rowcount

    def set_wallet_enabled(self, address: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE wallets SET enabled=?, updated_at=? WHERE address=?",
                         (1 if enabled else 0, _now_iso(), address))

    # ============ daily_profit ============

    def upsert_days(self, wallet: str, days: List[Dict[str, Any]],
                    source: str = "gmgn") -> int:
        """Upsert calendar days for a wallet. Returns rows written.

        Writes every field GMGN returns. `source` is kept for audit; GMGN is
        the only active source (debot is temporarily disabled).
        """
        now = _now_iso()
        n = 0
        with self._conn() as conn:
            for d in days:
                conn.execute("""
                    INSERT INTO daily_profit
                        (wallet, date, buy_count, sell_count,
                         transfer_in_count, transfer_out_count,
                         buy_volume, sell_volume,
                         transfer_in_amount_usd, transfer_out_amount_usd,
                         realized_profit, gains, losses,
                         win_sells, loss_sells,
                         last_trade_time, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet, date) DO UPDATE SET
                        buy_count=excluded.buy_count,
                        sell_count=excluded.sell_count,
                        transfer_in_count=excluded.transfer_in_count,
                        transfer_out_count=excluded.transfer_out_count,
                        buy_volume=excluded.buy_volume,
                        sell_volume=excluded.sell_volume,
                        transfer_in_amount_usd=excluded.transfer_in_amount_usd,
                        transfer_out_amount_usd=excluded.transfer_out_amount_usd,
                        realized_profit=excluded.realized_profit,
                        gains=excluded.gains,
                        losses=excluded.losses,
                        win_sells=excluded.win_sells,
                        loss_sells=excluded.loss_sells,
                        last_trade_time=excluded.last_trade_time,
                        synced_at=excluded.synced_at
                """, (wallet, d["date"], d["buy_count"], d["sell_count"],
                      int(d.get("transfer_in_count", 0) or 0),
                      int(d.get("transfer_out_count", 0) or 0),
                      d["buy_volume"], d["sell_volume"],
                      float(d.get("transfer_in_amount_usd", 0.0) or 0),
                      float(d.get("transfer_out_amount_usd", 0.0) or 0),
                      d["realized_profit"], d["gains"], d["losses"],
                      int(d.get("win_sells", 0) or 0),
                      int(d.get("loss_sells", 0) or 0),
                      d["last_trade_time"], now))

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
                start, end = self._month_range(month)
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

    def has_daily_data(self, wallet: str) -> bool:
        """该地址是否入库过任何一天的记录（不限月份）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_profit WHERE wallet=? LIMIT 1",
                (wallet,)).fetchone()
            return row is not None

    def get_recent_daily(self, wallet: str, days: int = 7) -> List[Dict[str, Any]]:
        """近 N 天滚动窗口的日记录 (UTC, 以当前时间往回推 N*24h)."""
        cutoff = _recent_cutoff(days)
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_profit
                WHERE wallet=? AND date>=? ORDER BY date
            """, (wallet, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def get_month_summary(self, wallet: str, month: str, days: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Get monthly summary. If days is provided, reuse it to avoid N+1 queries."""
        if days is None:
            days = self.get_daily(wallet, month)

        win_sells = sum(int(d.get("win_sells", 0) or 0) for d in days)
        loss_sells = sum(int(d.get("loss_sells", 0) or 0) for d in days)
        # 近 7 天盈利: 与月份无关, 以入库日记录按滚动 7 天窗口汇总
        recent_days = self.get_recent_daily(wallet, days=7)
        return {
            "wallet": wallet,
            "month": month,
            "days": len(days),
            "realized_profit": sum(d["realized_profit"] for d in days),
            "gains": sum(d["gains"] for d in days),
            "losses": sum(d["losses"] for d in days),
            "buy_count": sum(d["buy_count"] for d in days),
            "sell_count": sum(d["sell_count"] for d in days),
            "win_sells": win_sells,
            "loss_sells": loss_sells,
            "win_rate": _win_rate(win_sells, loss_sells),
            "pnl_7d": sum(d["realized_profit"] for d in recent_days),
        }

    def get_all_summaries(self, month: str) -> List[Dict[str, Any]]:
        """All enabled wallets in one aggregate query (no N+1)."""
        rows = self.get_summaries_page(month, limit=-1, offset=0)
        for row in rows:
            row["month"] = month
        return rows

    @staticmethod
    def _month_range(month: str) -> tuple:
        import calendar as _cal
        y, m = parse_month(month)
        start = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        last = _cal.monthrange(y, m)[1]
        end = int(datetime(y, m, last, 23, 59, 59,
                           tzinfo=timezone.utc).timestamp())
        return start, end

    def get_summaries_page(self, month: str, limit: int,
                           offset: int) -> List[Dict[str, Any]]:
        """One SQL: per-wallet month aggregates + label/enabled, paged."""
        start, end = self._month_range(month)
        cutoff_7d = _recent_cutoff(7)
        with self._conn() as conn:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(daily_profit)").fetchall()}
            win_select = (
                "COALESCE(SUM(d.win_sells), 0) AS win_sells, "
                "COALESCE(SUM(d.loss_sells), 0) AS loss_sells"
            ) if "win_sells" in cols and "loss_sells" in cols \
                else "0 AS win_sells, 0 AS loss_sells"
            rows = conn.execute(f"""
                SELECT w.address AS wallet, w.label AS label, w.enabled AS enabled,
                       COUNT(d.date) AS days,
                       COALESCE(SUM(d.realized_profit), 0) AS realized_profit,
                       COALESCE(SUM(d.gains), 0) AS gains,
                       COALESCE(SUM(d.losses), 0) AS losses,
                       COALESCE(SUM(d.buy_count), 0) AS buy_count,
                       COALESCE(SUM(d.sell_count), 0) AS sell_count,
                       COALESCE(SUM(d.buy_volume), 0) AS buy_volume,
                       COALESCE(SUM(d.sell_volume), 0) AS sell_volume,
                       COALESCE(SUM(d.transfer_in_count), 0) AS transfer_in_count,
                       COALESCE(SUM(d.transfer_out_count), 0) AS transfer_out_count,
                       COALESCE(SUM(d.transfer_in_amount_usd), 0) AS transfer_in_amount_usd,
                       COALESCE(SUM(d.transfer_out_amount_usd), 0) AS transfer_out_amount_usd,
                       {win_select},
                       COALESCE((
                           SELECT SUM(d7.realized_profit) FROM daily_profit d7
                           WHERE d7.wallet = w.address AND d7.date >= ?
                       ), 0) AS pnl_7d,
                       COALESCE((
                           SELECT MAX(s.updated_at) FROM sync_state s
                           WHERE s.wallet = w.address
                       ), (
                           SELECT MAX(d8.synced_at) FROM daily_profit d8
                           WHERE d8.wallet = w.address
                       )) AS updated_at
                FROM wallets w
                LEFT JOIN daily_profit d
                  ON d.wallet = w.address AND d.date >= ? AND d.date <= ?
                WHERE w.enabled = 1
                GROUP BY w.address
                ORDER BY w.created_at
                LIMIT ? OFFSET ?
            """, (cutoff_7d, start, end, limit, offset)).fetchall()
            out = []
            for r in rows:
                row = dict(r)
                row["win_rate"] = _win_rate(int(row.get("win_sells", 0) or 0),
                                            int(row.get("loss_sells", 0) or 0))
                out.append(row)
            return out

    def get_wallets_page(self, limit: int,
                         offset: int) -> List[Dict[str, Any]]:
        """地址管理列表: 基础信息 + 累计/近7天盈利 + 胜率 + 最近同步时间."""
        cutoff_7d = _recent_cutoff(7)
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT w.address AS address, w.label AS label,
                       w.enabled AS enabled,
                       w.created_at AS created_at, w.updated_at AS updated_at,
                       COALESCE((
                           SELECT SUM(d7.realized_profit) FROM daily_profit d7
                           WHERE d7.wallet = w.address AND d7.date >= ?
                       ), 0) AS pnl_7d,
                       COALESCE((
                           SELECT SUM(d10.realized_profit) FROM daily_profit d10
                           WHERE d10.wallet = w.address
                       ), 0) AS total_pnl,
                       COALESCE((
                           SELECT COUNT(*) FROM daily_profit d11
                           WHERE d11.wallet = w.address
                       ), 0) AS active_days,
                       COALESCE((
                           SELECT SUM(d8.win_sells) FROM daily_profit d8
                           WHERE d8.wallet = w.address
                       ), 0) AS win_sells,
                       COALESCE((
                           SELECT SUM(d8.loss_sells) FROM daily_profit d8
                           WHERE d8.wallet = w.address
                       ), 0) AS loss_sells,
                       COALESCE((
                           SELECT MAX(s.updated_at) FROM sync_state s
                           WHERE s.wallet = w.address
                       ), (
                           SELECT MAX(d9.synced_at) FROM daily_profit d9
                           WHERE d9.wallet = w.address
                       )) AS last_synced_at
                FROM wallets w
                ORDER BY w.created_at
                LIMIT ? OFFSET ?
            """, (cutoff_7d, limit, offset)).fetchall()
            out = []
            for r in rows:
                row = dict(r)
                row["win_rate"] = _win_rate(int(row.get("win_sells", 0) or 0),
                                            int(row.get("loss_sells", 0) or 0))
                out.append(row)
            return out

    def get_sync_stats(self) -> Dict[str, Any]:
        """同步进展: 已更新(有成功同步记录或任意入库记录)/未更新/总数."""
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN (
                        EXISTS (SELECT 1 FROM sync_state s
                                WHERE s.wallet = w.address AND s.status = 'ok')
                        OR EXISTS (SELECT 1 FROM daily_profit d
                                   WHERE d.wallet = w.address)
                    ) THEN 1 ELSE 0 END) AS synced
                FROM wallets w WHERE w.enabled = 1
            """).fetchone()
            total = int(row["total"] or 0) if row else 0
            synced = int(row["synced"] or 0) if row else 0
            return {"total": total, "synced": synced,
                    "unsynced": total - synced}

    def list_wallets_due(self, stale_minutes: int, limit: int) -> List[Dict[str, Any]]:
        """Enabled wallets whose latest sync attempt is older than the cutoff.

        Uses sync_state (not daily_profit) so wallets that legitimately have
        zero trades are still considered synced, and old sync_log pruning does
        not make them look unsynced. NULL last_synced wallets come first.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=max(stale_minutes, 1))).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT w.* FROM wallets w
                LEFT JOIN (
                    SELECT wallet, MAX(updated_at) AS last_synced_at
                    FROM sync_state GROUP BY wallet
                ) s ON s.wallet = w.address
                WHERE w.enabled = 1
                  AND (s.last_synced_at IS NULL OR s.last_synced_at < ?)
                ORDER BY CASE WHEN s.last_synced_at IS NULL THEN 0 ELSE 1 END,
                         s.last_synced_at
                LIMIT ?
            """, (cutoff, max(limit, 1))).fetchall()
            return [dict(r) for r in rows]

    def get_month_total(self, month: str) -> float:
        """Total realized profit across enabled wallets for the month."""
        start, end = self._month_range(month)
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(d.realized_profit), 0) AS total
                FROM daily_profit d
                JOIN wallets w ON w.address = d.wallet AND w.enabled = 1
                WHERE d.date >= ? AND d.date <= ?
            """, (start, end)).fetchone()
            return float(row["total"]) if row else 0.0

    # ============ sync_log ============

    def add_sync_log(self, wallet: str, month: str, days: int,
                     month_pnl: float, status: str = "ok",
                     message: str = "") -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO sync_log
                    (wallet, month, days, month_pnl, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (wallet, month, days, month_pnl, status, message, now))
            conn.execute("""
                INSERT INTO sync_state
                    (wallet, month, days, month_pnl, status, message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet, month) DO UPDATE SET
                    days=excluded.days, month_pnl=excluded.month_pnl,
                    status=excluded.status, message=excluded.message,
                    updated_at=excluded.updated_at
            """, (wallet, month, days, month_pnl, status, message, now))

    def get_sync_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_last_sync_log(self, wallet: str,
                          month: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Latest durable sync state (survives sync_log pruning).

        Falls back to sync_log for rows written before sync_state existed.
        """
        with self._conn() as conn:
            if month:
                row = conn.execute(
                    "SELECT * FROM sync_state WHERE wallet=? AND month=?",
                    (wallet, month)).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT * FROM sync_log WHERE wallet=? AND month=? "
                        "ORDER BY id DESC LIMIT 1",
                        (wallet, month)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sync_state WHERE wallet=? "
                    "ORDER BY updated_at DESC LIMIT 1", (wallet,)).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT * FROM sync_log WHERE wallet=? "
                        "ORDER BY id DESC LIMIT 1", (wallet,)).fetchone()
            return dict(row) if row else None

    def prune_sync_log(self, retention_days: int = 7) -> int:
        """Drop sync_log rows older than the retention window."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(retention_days, 1))).isoformat()
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sync_log WHERE created_at < ?",
                               (cutoff,))
            return cur.rowcount

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
