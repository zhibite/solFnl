"""GMGN daily_profits 接口客户端.

接口形态 (实测确认, 2026-10-27):
  POST https://gmgn.ai/pf/api/v1/wallets/{chain}/daily_profits
  body: {"wallet_addresses": ["<addr>", ...], "start_at": <unix秒>, "end_at": <unix秒>}
  resp: {"code": 0, "message": "success", "data": {"list": [ {...日记录...} ]}}

要点:
  - 是 POST, 不是 GET. 抓包里那种 `...?device_id=...` 的 URL 用 GET 打永远是 404.
  - 无需鉴权: 不带 token / 不带公共 query 参数都能拿到 code=0.
  - 时间跨度必须 < 1 年, 否则报 "time range must be less than 1 year".
  - start_at 必填, end_at 可选 (不给则取到今天).
  - **多钱包会求和合并**, 不能一次查多个地址. 要逐个钱包查.
  - 地址在响应里不带标识, 单钱包查询才安全.

字段 (与 docs/gmgn-daily.md 一一对应):
  date                    unix 秒, UTC 零点
  total_profit            当日已实现盈亏 (字符串, 需转 float)
  total_buys / total_sells            买入 / 卖出笔数
  total_transfer_ins / _outs          转入 / 转出笔数
  buy_amount_usd / sell_amount_usd    买入 / 卖出金额 (USD)
  transfer_in_amount_usd / transfer_out_amount_usd
  win_sells / loss_sells              盈利单数 / 亏损单数
  win_profit / loss_profit            盈利合计 / 亏损合计 (loss 为负)

以上字段全部入库；内部列名映射见 normalize()。
"""

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "https://gmgn.ai"
DAILY_PROFITS_PATH = "/pf/api/v1/wallets/{chain}/daily_profits"
COMMON_STAT_PATH = "/api/v1/wallet_common_stat/{chain}/{wallet}"
SMARTMONEY_PATH = "/defi/quotation/v1/smartmoney/{chain}/wallet/{wallet}"

#: 一年秒数 — 接口硬限制: time range must be less than 1 year
MAX_RANGE_SECONDS = 365 * 24 * 3600

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://gmgn.ai/",
    "Origin": "https://gmgn.ai",
}


class GMGNError(Exception):
    """GMGN 接口返回 code != 0, 或 HTTP/网络错误."""

    def __init__(self, code: int, message: str):
        super().__init__(f"gmgn api error code={code}: {message}")
        self.code = code
        self.message = message


def _to_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def normalize(item: Dict[str, Any]) -> Dict[str, Any]:
    """把接口返回的一条日记录转成规整的 dict（GMGN 格式到内部格式的映射）.
    
    GMGN 返回字段映射到内部字段（GMGN 是当前唯一启用的数据源）:
      total_profit            -> realized_profit
      total_buys              -> buy_count
      total_sells             -> sell_count
      total_transfer_ins      -> transfer_in_count
      total_transfer_outs     -> transfer_out_count
      buy_amount_usd          -> buy_volume
      sell_amount_usd         -> sell_volume
      transfer_in_amount_usd  -> transfer_in_amount_usd
      transfer_out_amount_usd -> transfer_out_amount_usd
      (win/loss_profit)       -> gains/losses
      win_sells / loss_sells 原样保留 (胜率 = win_sells / (win_sells+loss_sells))
    """
    return {
        "date": int(item.get("date", 0)),
        "buy_count": _to_int(item.get("total_buys")),
        "sell_count": _to_int(item.get("total_sells")),
        "transfer_in_count": _to_int(item.get("total_transfer_ins")),
        "transfer_out_count": _to_int(item.get("total_transfer_outs")),
        "buy_volume": _to_float(item.get("buy_amount_usd")),
        "sell_volume": _to_float(item.get("sell_amount_usd")),
        "transfer_in_amount_usd": _to_float(
            item.get("transfer_in_amount_usd")),
        "transfer_out_amount_usd": _to_float(
            item.get("transfer_out_amount_usd")),
        "realized_profit": _to_float(item.get("total_profit")),
        "gains": _to_float(item.get("win_profit")),
        "losses": _to_float(item.get("loss_profit")),
        "win_sells": _to_int(item.get("win_sells")),
        "loss_sells": _to_int(item.get("loss_sells")),
        "last_trade_time": 0,  # GMGN 无此字段，用 0
    }


class GMGNDailyClient:
    """GMGN 单钱包每日盈利客户端.

    用法:
        c = GMGNDailyClient()
        days = c.fetch_daily("215nhc...", "2026-09-01", "2026-10-01")
        print(sum(d["total_profit"] for d in days))
    """

    def __init__(self, base_url: str = BASE_URL, chain: str = "sol",
                 timeout: int = 25):
        self.base_url = base_url.rstrip("/")
        # GMGN expects "sol"; accept "solana" too and normalize.
        self.chain = "sol" if chain.lower() == "solana" else chain
        self.timeout = timeout

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=HEADERS,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise GMGNError(-e.code, f"HTTP {e.code}: {e.read()[:200]!r}")
        except Exception as e:  # noqa: BLE001
            raise GMGNError(-1, f"{type(e).__name__}: {e}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GMGNError(-1, f"invalid JSON: {e}, body={raw[:200]!r}")
        code = payload.get("code", -1)
        if code != 0:
            msg = (payload.get("message") or payload.get("reason")
                   or payload.get("msg") or "unknown")
            raise GMGNError(code, str(msg))
        return payload.get("data") or {}

    def _get(self, path: str) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers=HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise GMGNError(-e.code, f"HTTP {e.code}: {e.read()[:200]!r}")
        except Exception as e:  # noqa: BLE001
            raise GMGNError(-1, f"{type(e).__name__}: {e}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GMGNError(-1, f"invalid JSON: {e}, body={raw[:200]!r}")
        code = payload.get("code", -1)
        if code != 0:
            msg = (payload.get("message") or payload.get("reason")
                   or payload.get("msg") or "unknown")
            raise GMGNError(code, str(msg))
        return payload.get("data") or {}

    def fetch_daily(self, wallet: str, start_at: int,
                    end_at: Optional[int] = None) -> List[Dict[str, Any]]:
        """拉一个钱包的每日盈利.

        start_at / end_at 为 unix 秒.
        end_at 省略时取到当前时间.
        注意: 一次只能传一个钱包, 多个地址会被服务端求和合并.
        """
        if not wallet or not wallet.strip():
            raise ValueError("wallet is required")
        if end_at is not None and end_at - start_at >= MAX_RANGE_SECONDS:
            raise ValueError(
                f"time range must be less than 1 year "
                f"(got {end_at - start_at}s, max {MAX_RANGE_SECONDS}s)")
        body: Dict[str, Any] = {
            "wallet_addresses": [wallet.strip()],
            "start_at": int(start_at),
        }
        if end_at is not None:
            body["end_at"] = int(end_at)
        path = DAILY_PROFITS_PATH.format(chain=self.chain)
        data = self._post(path, body)
        return [normalize(d) for d in (data.get("list") or [])]

    def fetch_daily_many(self, wallets: List[str], start_at: int,
                         end_at: Optional[int] = None
                         ) -> Dict[str, List[Dict[str, Any]]]:
        """逐个钱包拉取, 返回 {wallet: [日记录...]}.

        服务端对多地址请求会求和合并, 所以这里串行单查.
        """
        out: Dict[str, List[Dict[str, Any]]] = {}
        for w in wallets:
            out[w] = self.fetch_daily(w, start_at, end_at)
        return out


def fetch_wallet_meta(wallet: str, chain: str = "sol",
                      timeout: int = 25) -> Dict[str, Any]:
    """Fetch public wallet-level stats from two GMGN endpoints.

    - /api/v1/wallet_common_stat/...  -> follow_count, remark_count, tags
    - /defi/quotation/v1/smartmoney/... -> 7d realized profit / buy / sell,
      balance, risk, etc. (no auth)

    Both endpoints are required: on any failure this raises so the caller
    keeps the previous snapshot instead of overwriting it with zeros.
    """
    client = GMGNDailyClient(chain=chain, timeout=timeout)
    errors: List[str] = []
    common: Dict[str, Any] = {}
    smart: Dict[str, Any] = {}
    try:
        common = client._get(COMMON_STAT_PATH.format(
            chain=client.chain, wallet=wallet))
    except GMGNError as e:
        errors.append(f"common_stat: {e}")
    try:
        smart = client._get(SMARTMONEY_PATH.format(
            chain=client.chain, wallet=wallet))
    except GMGNError as e:
        errors.append(f"smartmoney: {e}")
    if errors:
        raise GMGNError(-1, "; ".join(errors))

    return {
        "follow_count": _to_int(common.get("follow_count")),
        "remark_count": _to_int(common.get("remark_count")),
        "tags": common.get("tags") or [],
        "tag_rank": common.get("tag_rank") or {},
        "created_at": _to_int(common.get("created_at")),
        "fund_from_address": common.get("fund_from_address") or "",
        "realized_profit_7d": _to_float(smart.get("realized_profit_7d")),
        "pnl_7d": _to_float(smart.get("pnl_7d")),
        "buy_7d": _to_int(smart.get("buy_7d")),
        "sell_7d": _to_int(smart.get("sell_7d")),
        "balance": _to_float(smart.get("balance")),
        "total_profit": _to_float(smart.get("total_profit")),
        "total_profit_pnl": _to_float(smart.get("total_profit_pnl")),
        "all_pnl": _to_float(smart.get("all_pnl")),
        "last_active_timestamp": _to_int(smart.get("last_active_timestamp")),
        "risk": smart.get("risk") or {},
    }
