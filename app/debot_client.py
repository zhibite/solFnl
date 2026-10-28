"""debot.ai API client: wallet calendar (daily PnL) fetcher.

Auth: cookie beegosessionID (login via browser, paste value into config).
Network: requires non-CN egress IP (server side), plain requests works.
"""

import json
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

BASE_URL = "https://debot.ai"
CALENDAR_PATH = "/api/dashboard/wallet/calendar"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class DebotError(Exception):
    """code != 0 from debot API (101=not login, 112=bad params, ...)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"debot api error code={code}: {message}")
        self.code = code
        self.message = message


class DebotClient:
    """Thin wrapper over debot wallet calendar endpoint."""

    def __init__(self, session_id: str, chain: str = "solana",
                 timeout: int = 25):
        if not session_id:
            raise ValueError("session_id (beegosessionID) is required")
        self.session_id = session_id.strip()
        # Debot expects "solana"; accept "sol" too and normalize.
        self.chain = "solana" if chain.lower() == "sol" else chain
        self.timeout = timeout

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        q = dict(params)
        q.setdefault("request_id", str(uuid.uuid4()))
        url = f"{BASE_URL}{path}?" + urlencode(q)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": "https://debot.ai/",
            "Origin": "https://debot.ai",
            "Accept": "application/json",
            "Cookie": f"beegosessionID={self.session_id}",
        }, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise DebotError(-e.code, f"HTTP {e.code}: {e.read()[:200]!r}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise DebotError(-1, f"invalid JSON: {e}, body={body[:200]!r}")
        code = data.get("code", -1)
        if code != 0:
            msg = (data.get("message_cn") or data.get("message_en")
                   or data.get("message") or data.get("description") or "unknown")
            raise DebotError(code, str(msg))
        return data.get("data") or {}

    def fetch_calendar(self, wallet: str, month: str) -> List[Dict[str, Any]]:
        """Fetch one wallet x one month (month format 'YYYY-MM').

        Returns list of day dicts, fields mirror docs/debot.md:
        date, buy_count, sell_count, buy_volume, sell_volume,
        realized_profit, gains, losses, last_trade_time.
        win_sells/loss_sells 固定为 0 (Debot 无此字段, 胜率仅 GMGN 可用).
        """
        data = self._get(CALENDAR_PATH, {
            "chain": self.chain,
            "wallet": wallet,
            "month": month,
        })
        days = data.get("calendar") or []
        # normalize: ensure all fields exist with sane defaults
        out = []
        for d in days:
            out.append({
                "date": int(d.get("date", 0)),
                "buy_count": int(d.get("buy_count", 0)),
                "sell_count": int(d.get("sell_count", 0)),
                "buy_volume": float(d.get("buy_volume", 0.0)),
                "sell_volume": float(d.get("sell_volume", 0.0)),
                "realized_profit": float(d.get("realized_profit", 0.0)),
                "gains": float(d.get("gains", 0.0)),
                "losses": float(d.get("losses", 0.0)),
                "win_sells": 0,  # Debot 无此字段
                "loss_sells": 0,  # Debot 无此字段
                "last_trade_time": int(d.get("last_trade_time", 0)),
            })
        return out

    def check_session(self) -> bool:
        """Return True if session cookie is still valid."""
        try:
            self._get("/api/user/info", {})
            return True
        except DebotError as e:
            if e.code == 101:
                return False
            raise
