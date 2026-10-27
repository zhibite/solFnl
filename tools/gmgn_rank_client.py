"""GMGN 公开接口客户端.

只覆盖排行榜接口: /defi/quotation/v1/rank/sol/wallets/{timeframe}.

单钱包每日盈利历史见同目录 gmgn_daily_client.py
(POST /pf/api/v1/wallets/{chain}/daily_profits).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests
import urllib3

from .config import GMGN

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)


class GMGNError(Exception):
    """GMGN 接口错误基类."""


@dataclass
class RankItem:
    """排行榜里一个钱包的扁平视图.

    原始接口返回的字段非常零散, 很多字段 *_1d / *_7d / *_30d 复用了同一个语义,
    客户端不做二态校验, 直接把整条 dict 暴露给上层.
    """

    raw: dict[str, Any]

    @property
    def address(self) -> str:
        return self.raw.get("address") or self.raw.get("wallet_address") or ""

    @property
    def tags(self) -> list[str]:
        v = self.raw.get("tags")
        return list(v) if isinstance(v, list) else []

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def __contains__(self, key: str) -> bool:
        return key in self.raw

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


class GMGNClient:
    """GMGN 公开排行榜接口客户端.

    用法:
        with GMGNClient() as c:
            items = c.fetch_rank("7d")
            for it in items: ...
    """

    def __init__(
        self,
        base_url: str | None = None,
        common_qs: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_base_delay: float | None = None,
    ) -> None:
        cfg = GMGN
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.common_qs = dict(common_qs or cfg.common_qs)
        self.headers = dict(headers or cfg.headers)
        self.timeout = timeout if timeout is not None else cfg.timeout
        self.max_retries = max_retries if max_retries is not None else cfg.max_retries
        self.retry_base_delay = (
            retry_base_delay if retry_base_delay is not None else cfg.retry_base_delay
        )
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.verify = False  # GMGN 在某些地区 TLS 链异常, 跳过校验

    # ── 上下文管理 ──────────────────────────────────────────────
    def __enter__(self) -> "GMGNClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    # ── 内部 ────────────────────────────────────────────────────
    def _get(self, url: str) -> dict[str, Any]:
        """带退避重试的 GET, 返回 JSON body."""
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(
                    url, params=self.common_qs, timeout=self.timeout
                )
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        raise GMGNError(
                            f"非 JSON 响应 [{resp.status_code}] for {url}: "
                            + resp.text[:200]
                        )
                    if str(body.get("code", "0")) not in ("0", 0):
                        raise GMGNError(
                            f"业务错误 for {url}: code={body.get('code')} "
                            f"msg={body.get('msg') or body.get('reason') or ''}"
                        )
                    return body
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise GMGNError(f"transient status {resp.status_code}")
                raise GMGNError(
                    f"unexpected status {resp.status_code} for {url}: "
                    + resp.text[:200]
                )
            except (requests.RequestException, GMGNError) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                delay = self.retry_base_delay * (2 ** (attempt - 1))
                log.warning(
                    "GET %s 第 %d 次失败: %s, 等待 %.1fs 再试",
                    url, attempt, e, delay,
                )
                time.sleep(delay)
        raise GMGNError(
            f"重试 {self.max_retries} 次仍失败: {url} -> {last_err!r}"
        )

    # ── 公开 API ────────────────────────────────────────────────
    def fetch_rank(self, timeframe: str) -> list[RankItem]:
        """拉某一档排行榜. timeframe ∈ {1d, 7d, 30d}.

        返回 RankItem 列表, 每个 RankItem.raw 是原始 dict.
        """
        if timeframe not in GMGN.timeframes:
            raise ValueError(
                f"timeframe 必须是 {GMGN.timeframes} 之一, got {timeframe!r}"
            )
        url = f"{self.base_url}{GMGN.rank_endpoint}/{timeframe}"
        log.info("拉取排行榜: %s", url)
        body = self._get(url)
        data = body.get("data") or {}
        rank_list = data.get("rank") or []
        if not isinstance(rank_list, list):
            raise GMGNError(f"rank 字段不是数组: {type(rank_list)}")
        log.info("  -> 拿到 %d 个钱包", len(rank_list))
        return [RankItem(raw=item) for item in rank_list]

    def fetch_all_ranks(
        self, timeframes: tuple[str, ...] | None = None
    ) -> Iterator[tuple[str, list[RankItem]]]:
        """依次拉所有档位的排行榜, 生成 (timeframe, items) 流."""
        tfs = timeframes or GMGN.timeframes
        for tf in tfs:
            yield tf, self.fetch_rank(tf)


# CLI 调试
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with GMGNClient() as c:
        for tf, items in c.fetch_all_ranks():
            print(f"=== {tf}: {len(items)} wallets ===")
            for it in items[:3]:
                print(
                    f"  #{it.raw.get('rank','?')}  {it.address[:8]}… "
                    f"name={it.get('name')!r} "
                    f"pnl_7d={it.get('pnl_7d')} "
                    f"realized_7d={it.get('realized_profit_7d')}"
                )
