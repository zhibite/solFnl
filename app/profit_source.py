"""Profit data source abstraction & multi-source manager.

Supports dual-source fetching from GMGN and Debot, with configurable
default and fallback logic.
"""

from typing import Any, Dict, List, Optional, Literal
import logging

from debot_client import DebotClient, DebotError
from gmgn_client import GMGNDailyClient, GMGNError

log = logging.getLogger(__name__)

SourceType = Literal["debot", "gmgn"]

# Standard internal format for daily profit records
StandardRecord = Dict[str, Any]  # {date, buy_count, sell_count, ..., last_trade_time}


class DebotSource:
    """Adapter: Debot API -> standard format."""

    def __init__(self, session_id: str, chain: str = "solana"):
        if not session_id:
            raise ValueError("DEBOT_SESSION_ID not configured")
        self.client = DebotClient(session_id=session_id, chain=chain)
        self.name = "debot"

    def fetch_month(self, wallet: str, month: str) -> List[StandardRecord]:
        """Fetch one wallet x one month. Returns normalized records."""
        try:
            days = self.client.fetch_calendar(wallet, month)
            # Debot data is already in standard format
            return days
        except DebotError as e:
            if e.code == 101:
                raise  # Session expired, let caller handle
            raise

    def check_available(self) -> bool:
        """Test if session is valid."""
        try:
            return self.client.check_session()
        except Exception:
            return False


class GMGNSource:
    """Adapter: GMGN API -> standard format."""

    def __init__(self, chain: str = "sol"):
        self.client = GMGNDailyClient(chain=chain)
        self.name = "gmgn"

    def fetch_month(self, wallet: str, month: str) -> List[StandardRecord]:
        """Fetch one wallet x one month. Returns normalized records."""
        try:
            from datetime import datetime
            from calendar import monthrange
            from datetime import timezone
            
            y, m = map(int, month.split("-"))
            start_dt = datetime(y, m, 1, 0, 0, 0, tzinfo=timezone.utc)
            last_day = monthrange(y, m)[1]
            end_dt = datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc)
            
            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())
            
            days = self.client.fetch_daily(wallet, start_ts, end_ts)
            # Already normalized to standard format by gmgn_client.normalize()
            return days
        except GMGNError as e:
            raise

    def check_available(self) -> bool:
        """GMGN has no auth, always available."""
        return True


class ProfitSourceManager:
    """Multi-source profit fetcher with configurable default & fallback."""

    def __init__(self, debot_session_id: Optional[str] = None,
                 default_source: SourceType = "gmgn",
                 enable_gmgn: bool = True,
                 enable_debot: bool = True,
                 chain: str = "solana"):
        """
        debot_session_id: beegosessionID cookie value
        default_source: which source to prefer (gmgn or debot)
        enable_gmgn/enable_debot: enable each source
        chain: blockchain name for APIs
        """
        self.default_source = default_source
        self.enable_gmgn = enable_gmgn
        self.enable_debot = enable_debot
        self.chain = chain
        
        self._debot: Optional[DebotSource] = None
        self._gmgn: Optional[GMGNSource] = None
        
        # Initialize sources
        if enable_gmgn:
            self._gmgn = GMGNSource(chain=chain)
        if enable_debot and debot_session_id:
            try:
                self._debot = DebotSource(session_id=debot_session_id, chain=chain)
            except Exception as e:
                log.warning("Failed to initialize Debot source: %s", e)
                self._debot = None

    def get_available_sources(self) -> List[SourceType]:
        """List currently available sources."""
        sources = []
        if self.enable_gmgn and self._gmgn and self._gmgn.check_available():
            sources.append("gmgn")
        if self.enable_debot and self._debot and self._debot.check_available():
            sources.append("debot")
        return sources

    def fetch_month(self, wallet: str, month: str,
                    source: Optional[SourceType] = None,
                    fallback: bool = True) -> tuple[List[StandardRecord], SourceType]:
        """
        Fetch profit data for one wallet x month.
        
        source: specific source to use, default self.default_source
        fallback: if True, try other sources on failure
        
        Returns: (records, actual_source_used)
        """
        source = source or self.default_source
        
        if source == "gmgn":
            sources_order = ["gmgn", "debot"] if fallback else ["gmgn"]
        else:  # debot
            sources_order = ["debot", "gmgn"] if fallback else ["debot"]
        
        last_error = None
        for src in sources_order:
            if src == "gmgn":
                if not self.enable_gmgn or not self._gmgn:
                    continue
                try:
                    records = self._gmgn.fetch_month(wallet, month)
                    log.info("[SOURCE] fetched %s/%s from gmgn (%d days)",
                             wallet[:8], month, len(records))
                    return records, "gmgn"
                except Exception as e:
                    log.warning("[SOURCE] gmgn fetch failed: %s", e)
                    last_error = e
                    if fallback:
                        continue
                    raise
            else:  # debot
                if not self.enable_debot or not self._debot:
                    continue
                try:
                    records = self._debot.fetch_month(wallet, month)
                    log.info("[SOURCE] fetched %s/%s from debot (%d days)",
                             wallet[:8], month, len(records))
                    return records, "debot"
                except Exception as e:
                    log.warning("[SOURCE] debot fetch failed: %s", e)
                    last_error = e
                    if fallback and src != sources_order[-1]:
                        continue
                    raise
        
        if last_error:
            raise last_error
        raise RuntimeError(f"No profit sources available for {source}")

    def fetch_month_both(self, wallet: str, month: str) -> tuple[
            Dict[str, List[StandardRecord]], Dict[str, str]]:
        """Fetch from both sources (when available). Returns records dict + status."""
        results = {}
        sources = {}
        
        if self.enable_gmgn and self._gmgn:
            try:
                results["gmgn"] = self._gmgn.fetch_month(wallet, month)
                sources["gmgn"] = "ok"
                log.info("[SOURCE] gmgn dual-fetch ok: %s/%s",
                         wallet[:8], month)
            except Exception as e:
                sources["gmgn"] = f"error: {e}"
                log.warning("[SOURCE] gmgn dual-fetch failed: %s", e)
        
        if self.enable_debot and self._debot:
            try:
                results["debot"] = self._debot.fetch_month(wallet, month)
                sources["debot"] = "ok"
                log.info("[SOURCE] debot dual-fetch ok: %s/%s",
                         wallet[:8], month)
            except Exception as e:
                sources["debot"] = f"error: {e}"
                log.warning("[SOURCE] debot dual-fetch failed: %s", e)
        
        return results, sources
