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

# Selected as a 1:1 LEFT JOIN with alias `m` and prefixed `meta_*` so both
# list pages and API responses can expose the same wallet metadata.
_META_SELECT = """
    m.follow_count AS meta_follow_count,
    m.remark_count AS meta_remark_count,
    m.tags_json AS meta_tags_json,
    m.tag_rank_json AS meta_tag_rank_json,
    m.gmgn_created_at AS meta_gmgn_created_at,
    m.fund_from_address AS meta_fund_from_address,
    m.realized_profit_7d AS meta_realized_profit_7d,
    m.pnl_7d AS meta_pnl_7d,
    m.buy_7d AS meta_buy_7d,
    m.sell_7d AS meta_sell_7d,
    m.balance AS meta_balance,
    m.total_profit AS meta_total_profit,
    m.total_profit_pnl AS meta_total_profit_pnl,
    m.all_pnl AS meta_all_pnl,
    m.last_active_timestamp AS meta_last_active_timestamp,
    m.risk_json AS meta_risk_json,
    m.fetched_at AS meta_fetched_at,
    m.last_error AS meta_last_error,
    m.has_data AS meta_has_data
"""

_SCORE_SELECT = """
    sc.score AS score_value,
    sc.grade AS score_grade,
    sc.detail_json AS score_detail_json,
    sc.window_days AS score_window_days,
    sc.meta_available AS score_meta_available,
    sc.updated_at AS score_updated_at
"""


def _json_load(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


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
            # Public wallet-level stats fetched from GMGN
            # (wallet_common_stat + smartmoney). Refreshed on a slower cadence
            # than daily_profit; see META_SYNC_INTERVAL_MIN.
            c.execute("""
                CREATE TABLE IF NOT EXISTS wallet_meta (
                    wallet TEXT PRIMARY KEY,
                    follow_count INTEGER NOT NULL DEFAULT 0,
                    remark_count INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    tag_rank_json TEXT NOT NULL DEFAULT '{}',
                    gmgn_created_at INTEGER NOT NULL DEFAULT 0,
                    fund_from_address TEXT NOT NULL DEFAULT '',
                    realized_profit_7d REAL NOT NULL DEFAULT 0,
                    pnl_7d REAL NOT NULL DEFAULT 0,
                    buy_7d INTEGER NOT NULL DEFAULT 0,
                    sell_7d INTEGER NOT NULL DEFAULT 0,
                    balance REAL NOT NULL DEFAULT 0,
                    total_profit REAL NOT NULL DEFAULT 0,
                    total_profit_pnl REAL NOT NULL DEFAULT 0,
                    all_pnl REAL NOT NULL DEFAULT 0,
                    last_active_timestamp INTEGER NOT NULL DEFAULT 0,
                    risk_json TEXT NOT NULL DEFAULT '{}',
                    has_data INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (wallet) REFERENCES wallets(address)
                        ON DELETE CASCADE
                )
            """)
            # Computed wallet quality score (0-100) and its breakdown.
            c.execute("""
                CREATE TABLE IF NOT EXISTS wallet_score (
                    wallet TEXT PRIMARY KEY,
                    score REAL NOT NULL DEFAULT 0,
                    grade TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    window_days INTEGER NOT NULL DEFAULT 90,
                    meta_available INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (wallet) REFERENCES wallets(address)
                        ON DELETE CASCADE
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
            meta_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(wallet_meta)").fetchall()}
            if "has_data" not in meta_cols:
                conn.execute("ALTER TABLE wallet_meta ADD COLUMN "
                             "has_data INTEGER NOT NULL DEFAULT 0")
            score_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(wallet_score)").fetchall()}
            if "meta_available" not in score_cols:
                conn.execute("ALTER TABLE wallet_score ADD COLUMN "
                             "meta_available INTEGER NOT NULL DEFAULT 0")

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
            conn.execute("DELETE FROM wallet_meta WHERE wallet=?", (address,))
            conn.execute("DELETE FROM wallet_score WHERE wallet=?", (address,))
            cur = conn.execute("DELETE FROM wallets WHERE address=?", (address,))
            return cur.rowcount

    def set_wallet_enabled(self, address: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE wallets SET enabled=?, updated_at=? WHERE address=?",
                         (1 if enabled else 0, _now_iso(), address))

    # ============ wallet_meta (GMGN public stats) ============

    def upsert_wallet_meta(self, wallet: str, meta: Dict[str, Any]) -> None:
        """Store a complete wallet_common_stat + smartmoney snapshot."""
        now = _now_iso()
        tags = json.dumps(meta.get("tags") or [], ensure_ascii=False)
        tag_rank = json.dumps(meta.get("tag_rank") or {}, ensure_ascii=False)
        risk = json.dumps(meta.get("risk") or {}, ensure_ascii=False)
        fetched_at = meta.get("fetched_at") or now
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO wallet_meta (
                    wallet, follow_count, remark_count, tags_json, tag_rank_json,
                    gmgn_created_at, fund_from_address, realized_profit_7d,
                    pnl_7d, buy_7d, sell_7d, balance, total_profit,
                    total_profit_pnl, all_pnl, last_active_timestamp,
                    risk_json, has_data, fetched_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    follow_count=excluded.follow_count,
                    remark_count=excluded.remark_count,
                    tags_json=excluded.tags_json,
                    tag_rank_json=excluded.tag_rank_json,
                    gmgn_created_at=excluded.gmgn_created_at,
                    fund_from_address=excluded.fund_from_address,
                    realized_profit_7d=excluded.realized_profit_7d,
                    pnl_7d=excluded.pnl_7d,
                    buy_7d=excluded.buy_7d,
                    sell_7d=excluded.sell_7d,
                    balance=excluded.balance,
                    total_profit=excluded.total_profit,
                    total_profit_pnl=excluded.total_profit_pnl,
                    all_pnl=excluded.all_pnl,
                    last_active_timestamp=excluded.last_active_timestamp,
                    risk_json=excluded.risk_json,
                    has_data=excluded.has_data,
                    fetched_at=excluded.fetched_at,
                    last_error=excluded.last_error
            """, (
                wallet,
                int(meta.get("follow_count") or 0),
                int(meta.get("remark_count") or 0),
                tags, tag_rank,
                int(meta.get("created_at") or 0),
                str(meta.get("fund_from_address") or ""),
                float(meta.get("realized_profit_7d") or 0),
                float(meta.get("pnl_7d") or 0),
                int(meta.get("buy_7d") or 0),
                int(meta.get("sell_7d") or 0),
                float(meta.get("balance") or 0),
                float(meta.get("total_profit") or 0),
                float(meta.get("total_profit_pnl") or 0),
                float(meta.get("all_pnl") or 0),
                int(meta.get("last_active_timestamp") or 0),
                risk, 1, fetched_at,
                str(meta.get("last_error") or "")[:500],
            ))

    def mark_wallet_meta_error(self, wallet: str, error: str) -> None:
        """Record a failed fetch without erasing the last good snapshot."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO wallet_meta (wallet, has_data, fetched_at, last_error)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    fetched_at=excluded.fetched_at,
                    last_error=excluded.last_error
            """, (wallet, _now_iso(), str(error)[:500]))

    def list_wallets_meta_due(self, stale_minutes: int, limit: int
                              ) -> List[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(
            minutes=max(stale_minutes, 1))).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT w.address FROM wallets w
                LEFT JOIN wallet_meta m ON m.wallet = w.address
                WHERE w.enabled=1
                  AND (m.wallet IS NULL OR m.fetched_at IS NULL
                       OR m.fetched_at < ?)
                ORDER BY CASE WHEN m.fetched_at IS NULL THEN 0 ELSE 1 END,
                         m.fetched_at
                LIMIT ?
            """, (cutoff, max(limit, 1))).fetchall()
            return [r["address"] for r in rows]

    def get_wallet_meta(self, wallet: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wallet_meta WHERE wallet=?", (wallet,)).fetchone()
        if not row:
            return None
        d = dict(row)
        return {
            "follow_count": int(d.get("follow_count") or 0),
            "remark_count": int(d.get("remark_count") or 0),
            "tags": _json_load(d.get("tags_json"), []),
            "tag_rank": _json_load(d.get("tag_rank_json"), {}),
            "created_at": int(d.get("gmgn_created_at") or 0),
            "fund_from_address": d.get("fund_from_address") or "",
            "realized_profit_7d": float(d.get("realized_profit_7d") or 0),
            "pnl_7d": float(d.get("pnl_7d") or 0),
            "buy_7d": int(d.get("buy_7d") or 0),
            "sell_7d": int(d.get("sell_7d") or 0),
            "balance": float(d.get("balance") or 0),
            "total_profit": float(d.get("total_profit") or 0),
            "total_profit_pnl": float(d.get("total_profit_pnl") or 0),
            "all_pnl": float(d.get("all_pnl") or 0),
            "last_active_timestamp": int(d.get("last_active_timestamp") or 0),
            "risk": _json_load(d.get("risk_json"), {}),
            "has_data": int(d.get("has_data") or 0),
            "fetched_at": d.get("fetched_at"),
            "last_error": d.get("last_error") or "",
        }

    def list_wallets_with_meta(self, only_enabled: bool = False,
                               limit: Optional[int] = None,
                               offset: int = 0) -> List[Dict[str, Any]]:
        where = "WHERE w.enabled=1" if only_enabled else ""
        sql = f"""
            SELECT w.*, {_META_SELECT}, {_SCORE_SELECT}
            FROM wallets w
            LEFT JOIN wallet_meta m ON m.wallet = w.address
            LEFT JOIN wallet_score sc ON sc.wallet = w.address
            {where}
            ORDER BY w.created_at
        """
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [max(limit, 1), max(offset, 0)]
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ============ wallet_score ============

    def get_daily_window(self, wallet: str, days: int = 90
                         ) -> List[Dict[str, Any]]:
        """Calendar rows inside the rolling scoring window."""
        cutoff = _recent_cutoff(days)
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT date, realized_profit, gains, losses,
                       win_sells, loss_sells, buy_count, sell_count
                FROM daily_profit
                WHERE wallet=? AND date>=? ORDER BY date
            """, (wallet, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def upsert_wallet_score(self, wallet: str,
                            result: Dict[str, Any]) -> None:
        detail = json.dumps(result, ensure_ascii=False)
        meta_available = 1 if (result.get("metrics") or {}).get(
            "meta_available") else 0
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO wallet_score
                    (wallet, score, grade, detail_json, window_days,
                     meta_available, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    score=excluded.score,
                    grade=excluded.grade,
                    detail_json=excluded.detail_json,
                    window_days=excluded.window_days,
                    meta_available=excluded.meta_available,
                    updated_at=excluded.updated_at
            """, (wallet, float(result.get("score") or 0),
                  str(result.get("grade") or ""), detail,
                  int(result.get("window_days") or 90),
                  meta_available, _now_iso()))

    def get_wallet_score(self, wallet: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wallet_score WHERE wallet=?", (wallet,)
            ).fetchone()
        if not row:
            return None
        detail = _json_load(row["detail_json"], {})
        return {
            "value": round(float(row["score"] or 0), 1),
            "grade": row["grade"] or "",
            "window_days": int(row["window_days"] or 90),
            "meta_available": int(row["meta_available"] or 0),
            "components": detail.get("components") or {},
            "metrics": detail.get("metrics") or {},
            "updated_at": row["updated_at"],
        }

    def list_wallets_missing_score(self, limit: int = 200
                                   ) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT w.address FROM wallets w
                LEFT JOIN wallet_score sc ON sc.wallet = w.address
                WHERE w.enabled=1 AND sc.wallet IS NULL
                  AND (
                    EXISTS (SELECT 1 FROM daily_profit d
                            WHERE d.wallet = w.address)
                    OR EXISTS (SELECT 1 FROM wallet_meta m
                               WHERE m.wallet = w.address)
                  )
                ORDER BY w.created_at
                LIMIT ?
            """, (max(limit, 1),)).fetchall()
            return [r["address"] for r in rows]

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
        today_start = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_end = today_start + 86399
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
                       {_META_SELECT},
                       {_SCORE_SELECT},
                       {win_select},
                       COALESCE((
                           SELECT SUM(d7.realized_profit) FROM daily_profit d7
                           WHERE d7.wallet = w.address AND d7.date >= ?
                       ), 0) AS pnl_7d,
                       COALESCE((
                           SELECT SUM(d6.realized_profit) FROM daily_profit d6
                           WHERE d6.wallet = w.address
                             AND d6.date >= ? AND d6.date <= ?
                       ), 0) AS today_pnl,
                       COALESCE((
                           SELECT MAX(s.updated_at) FROM sync_state s
                           WHERE s.wallet = w.address
                       ), (
                           SELECT MAX(d8.synced_at) FROM daily_profit d8
                           WHERE d8.wallet = w.address
                       )) AS updated_at
                FROM wallets w
                LEFT JOIN wallet_meta m ON m.wallet = w.address
                LEFT JOIN wallet_score sc ON sc.wallet = w.address
                LEFT JOIN daily_profit d
                  ON d.wallet = w.address AND d.date >= ? AND d.date <= ?
                WHERE w.enabled = 1
                GROUP BY w.address
                ORDER BY w.created_at DESC, w.address ASC
                LIMIT ? OFFSET ?
            """, (cutoff_7d, today_start, today_end, start, end,
                  limit, offset)).fetchall()
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
            rows = conn.execute(f"""
                SELECT w.address AS address, w.label AS label,
                       w.enabled AS enabled,
                       w.created_at AS created_at, w.updated_at AS updated_at,
                       {_META_SELECT},
                       {_SCORE_SELECT},
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
                LEFT JOIN wallet_meta m ON m.wallet = w.address
                LEFT JOIN wallet_score sc ON sc.wallet = w.address
                ORDER BY w.created_at DESC, w.address ASC
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

    def list_sync_targets(self, months: List[str],
                          current_interval_min: int,
                          history_interval_min: int,
                          limit: int) -> List[Dict[str, Any]]:
        """Return wallets with their due months, oldest sync first.

        The current month (months[0]) is due after `current_interval_min`
        minutes; older months after `history_interval_min` minutes. A wallet
        is returned once with only its due months, so a fresh current month
        does not force two extra historical requests.
        """
        if not months:
            return []
        now = datetime.now(timezone.utc)
        current_cutoff = (now - timedelta(
            minutes=max(current_interval_min, 1))).isoformat()
        history_cutoff = (now - timedelta(
            minutes=max(history_interval_min, 1))).isoformat()

        values_sql = ", ".join("(?, ?, ?)" for _ in months)
        params: List[Any] = []
        for idx, month in enumerate(months):
            params.extend([month, 1 if idx == 0 else 0, idx])
        # One wallet may have up to len(months) due rows; this guarantees
        # enough rows to group into `limit` wallets.
        row_limit = max(limit, 1) * len(months)

        sql = f"""
            WITH months(month, is_current, ord) AS (
                VALUES {values_sql}
            ),
            due AS (
                SELECT w.address AS wallet, m.month AS month,
                       m.is_current AS is_current, m.ord AS ord,
                       s.updated_at AS last_synced
                FROM wallets w
                CROSS JOIN months m
                LEFT JOIN sync_state s
                  ON s.wallet = w.address AND s.month = m.month
                WHERE w.enabled = 1
                  AND (s.updated_at IS NULL
                       OR (m.is_current = 1 AND s.updated_at < ?)
                       OR (m.is_current = 0 AND s.updated_at < ?))
            )
            SELECT wallet, month, is_current, last_synced
            FROM due
            ORDER BY CASE WHEN last_synced IS NULL THEN 0 ELSE 1 END,
                     last_synced,
                     is_current DESC,
                     wallet,
                     ord
            LIMIT ?
        """
        params.extend([current_cutoff, history_cutoff, row_limit])
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        targets: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in rows:
            wallet = row["wallet"]
            if wallet not in targets:
                targets[wallet] = {"address": wallet, "due_months": []}
                order.append(wallet)
            targets[wallet]["due_months"].append(row["month"])
        return [targets[w] for w in order[:max(limit, 1)]]

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
