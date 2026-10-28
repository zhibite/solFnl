"""gmgndaily - debot sol wallet daily PnL crawler + web console.

Run:  cd app && python app.py   (or: uvicorn app:app --port 8038)
Data source: GMGN only (debot is temporarily disabled).
Env:  DATABASE_PATH (default /data/gmgndaily.db)
      GMGN_PASSWORD (web console login; required in production)
      SESSION_SECRET (cookie signing key; set for stable sessions)
      SYNC_INTERVAL_MIN (default 60), SYNC_MONTHS_BACK (default 2)
      SYNC_TICK_SECONDS (default 60), SYNC_BATCH_SIZE (default 20)
      SYNC_LOG_RETENTION_DAYS (default 7), TRACK_MAX_CONCURRENT (default 4)
"""

import asyncio
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from database import get_db
from profit_source import ProfitSourceManager
from validators import is_valid_month, is_valid_sol_address

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("gmgndaily")

# ============ config ============
SYNC_INTERVAL_MIN = int(os.getenv("SYNC_INTERVAL_MIN", "60"))
SYNC_MONTHS_BACK = int(os.getenv("SYNC_MONTHS_BACK", "2"))
SYNC_TICK_SECONDS = max(int(os.getenv("SYNC_TICK_SECONDS", "60")), 5)
SYNC_BATCH_SIZE = max(int(os.getenv("SYNC_BATCH_SIZE", "20")), 1)
SYNC_LOG_RETENTION_DAYS = max(int(os.getenv("SYNC_LOG_RETENTION_DAYS", "7")), 1)
TRACK_MAX_CONCURRENT = max(int(os.getenv("TRACK_MAX_CONCURRENT", "4")), 1)
CHAIN = os.getenv("CHAIN", "solana")
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ITEMS = 50000

AUTH_USERNAME = "zengsw"
# No hard-coded default password: a missing/empty env gets a one-off random
# password printed at startup. Set GMGN_PASSWORD in production.
_AUTH_PASSWORD_ENV = os.getenv("GMGN_PASSWORD", "").strip()
AUTH_PASSWORD = _AUTH_PASSWORD_ENV or secrets.token_urlsafe(12)
if not _AUTH_PASSWORD_ENV:
    log.warning("[AUTH] GMGN_PASSWORD is not set; generated password for "
                "this process: %s", AUTH_PASSWORD)

_SESSION_SECRET_ENV = os.getenv("SESSION_SECRET", "").strip()
SESSION_SECRET = _SESSION_SECRET_ENV or secrets.token_urlsafe(32)
if not _SESSION_SECRET_ENV:
    log.warning("[AUTH] SESSION_SECRET is not set; sessions will be "
                "invalidated on every restart")

# Simple in-process login failure throttle. A global bucket is deliberate:
# behind a reverse proxy every request shares the proxy IP, and client
# X-Forwarded-For values are spoofable, so per-IP keys are unreliable.
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_FAILURES = 10
_login_failures: List[float] = []


def is_authed(request: Request) -> bool:
    return bool(request.session.get("user"))


def _login_blocked() -> bool:
    now = time.monotonic()
    _login_failures[:] = [t for t in _login_failures
                          if now - t < LOGIN_WINDOW_SECONDS]
    return len(_login_failures) >= LOGIN_MAX_FAILURES


def _record_login_failure() -> None:
    _login_failures.append(time.monotonic())


class NeedLogin(Exception):
    def __init__(self, location: str):
        self.location = location


def require_login(request: Request):
    if not is_authed(request):
        nxt = request.url.path
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        raise NeedLogin(f"/login?next={quote(nxt, safe='')}")
    return request


# ============ app ============
# NOTE: lifespan must be passed to the FastAPI constructor (setting
# app.router.lifespan_context after the fact is ignored in FastAPI>=0.115).
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sync_task
    _sync_task = asyncio.create_task(_sync_loop())
    log.info("[START] gmgndaily ready")
    yield
    for task in (_sync_task, _full_sync_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    log.info("[STOP] gmgndaily stopped")


app = FastAPI(title="gmgndaily", version="1.0.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   session_cookie="gmgndaily_session",
                   max_age=60 * 60 * 24 * 7, same_site="lax")


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse(url=exc.location, status_code=302)


BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _make_json_safe(obj: Any) -> Any:
    """Convert objects to JSON-safe types for Jinja2 rendering."""
    if isinstance(obj, dict):
        return {str(k): _make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_safe(item) for item in obj]
    elif obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    else:
        return str(obj)


def _body_dict(body: Any) -> Dict[str, Any]:
    """JSON bodies must be objects; anything else is treated as empty."""
    return body if isinstance(body, dict) else {}


def _ts_to_date(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)


def _fmt_money(value: Any, digits: int = 2) -> str:
    """1,234,567.89 style formatting for templates."""
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["timestamp_to_date"] = _ts_to_date
templates.env.filters["money"] = lambda v: _fmt_money(v, 2)
templates.env.filters["money0"] = lambda v: _fmt_money(v, 0)


def _source_manager() -> ProfitSourceManager:
    """GMGN is the only active source (debot is temporarily disabled)."""
    return ProfitSourceManager(default_source="gmgn", enable_gmgn=True,
                               enable_debot=False, chain=CHAIN)


def _target_months(n_back: int = SYNC_MONTHS_BACK) -> List[str]:
    """Current month + n_back previous months, 'YYYY-MM' format."""
    now = datetime.now(timezone.utc)
    out = []
    y, m = now.year, now.month
    for _ in range(n_back + 1):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _page_param(request: Request, default: int = 1) -> int:
    """Read ?page= safely (non-numeric input falls back to `default`)."""
    try:
        page = int(request.query_params.get("page", default))
    except (TypeError, ValueError):
        return default
    return max(page, 1)


def _month_param(request: Request) -> str:
    """Read ?month= and fall back to the current month when invalid."""
    month = request.query_params.get("month") or ""
    return month if is_valid_month(month) else _target_months(0)[0]


# ============ sync engine ============
_sync_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_run": None,
    "last_result": None,
    "progress": {"done": 0, "total": 0},
}
_sync_task: Optional[asyncio.Task] = None
_full_sync_task: Optional[asyncio.Task] = None

# Bounds concurrent /api/track syncs so public callers can't spawn unbounded
# worker threads (each call may make up to 3 upstream requests).
_track_inflight = 0
_track_lock = asyncio.Lock()


def _begin_sync() -> bool:
    """Atomically claim the single sync slot (runs on the event loop)."""
    if _sync_state["running"]:
        return False
    _sync_state["running"] = True
    _sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _sync_state["progress"] = {"done": 0, "total": 0}
    return True


def _end_sync() -> None:
    _sync_state["running"] = False


def sync_one_wallet(wallet: str, months: List[str]) -> Dict[str, Any]:
    """Blocking sync of one wallet over given months. Returns per-month result."""
    db = get_db()
    sources_mgr = _source_manager()
    total_days = 0
    per_month = []

    for month in months:
        try:
            days, source = sources_mgr.fetch_month(wallet, month, fallback=True)
            n = db.upsert_days(wallet, days, source=source)
            pnl = sum(d["realized_profit"] for d in days)
            db.add_sync_log(wallet, month, len(days), pnl, "ok",
                            f"source={source}")
            total_days += n
            per_month.append({"month": month, "days": len(days), "pnl": pnl,
                              "source": source})
        except Exception as e:
            code = getattr(e, "code", None)
            err_msg = str(e)
            if code is not None:
                err_msg = f"code={code} {getattr(e, 'message', str(e))}"
            db.add_sync_log(wallet, month, 0, 0.0, "error", err_msg)
            per_month.append({"month": month, "error": err_msg,
                              "error_code": code})
            log.warning("[SYNC] %s/%s failed: %s", wallet[:8], month, err_msg)

    return {"wallet": wallet, "total_days": total_days, "months": per_month}


def _session_expired(result: Dict[str, Any]) -> bool:
    return any(m.get("error_code") == 101 for m in result.get("months", []))


def _summarize_results(results: List[Dict[str, Any]], months: List[str],
                       session_expired: bool = False) -> Dict[str, Any]:
    """Compact run summary (never keep the full per-wallet list in memory
    across ticks, and never return it from the status endpoint)."""
    ok_wallets = sum(1 for r in results
                     if any("error" not in m for m in r.get("months", [])))
    errors = [
        {"wallet": r["wallet"], "month": m["month"], "error": m["error"]}
        for r in results for m in r.get("months", []) if "error" in m
    ][:50]
    return {
        "months": months,
        "wallets": len(results),
        "ok": ok_wallets,
        "failed": len(results) - ok_wallets,
        "total_days": sum(r.get("total_days", 0) for r in results),
        "session_expired": session_expired,
        "errors": errors,
    }


def _sync_wallet_list(wallets: List[Dict[str, Any]],
                      months: List[str]) -> Dict[str, Any]:
    """Sync a list of wallets sequentially, tracking progress + 101 abort."""
    _sync_state["progress"] = {"done": 0, "total": len(wallets)}
    results: List[Dict[str, Any]] = []
    expired = False
    for i, w in enumerate(wallets):
        address = w["address"]
        try:
            result = sync_one_wallet(address, months)
        except Exception as e:  # defensive: never abort the whole batch
            code = getattr(e, "code", None)
            result = {"wallet": address, "total_days": 0,
                      "months": [{"month": m, "error": str(e),
                                  "error_code": code} for m in months]}
            # Record the attempt so a permanently failing wallet does not
            # stay at the head of the queue and hot-loop every tick.
            try:
                for m in months:
                    get_db().add_sync_log(address, m, 0, 0.0, "error", str(e))
            except Exception:
                log.warning("[SYNC] could not record failure for %s",
                            address[:8])
        results.append(result)
        _sync_state["progress"]["done"] = i + 1
        if _session_expired(result):
            expired = True
            log.warning("[SYNC] debot session expired (code=101); "
                        "stopping this run")
            break
    return _summarize_results(results, months, expired)


def sync_all(months: Optional[List[str]] = None,
             wallets: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Sync an explicit (or all enabled) wallet list. Blocking."""
    db = get_db()
    months = months or _target_months()
    if wallets is None:
        wallets = db.list_wallets(only_enabled=True)
    summary = _sync_wallet_list(wallets, months)
    _sync_state["last_run"] = datetime.now(timezone.utc).isoformat()
    _sync_state["last_result"] = summary
    return summary


def sync_due_batch(limit: Optional[int] = None) -> Dict[str, Any]:
    """Sync at most `limit` wallets whose last attempt is older than the
    configured interval. Blocking; used by the scheduler queue."""
    db = get_db()
    months = _target_months()
    wallets = db.list_wallets_due(SYNC_INTERVAL_MIN,
                                  limit or SYNC_BATCH_SIZE)
    if not wallets:
        return {"months": months, "wallets": 0, "ok": 0, "failed": 0,
                "total_days": 0, "session_expired": False, "errors": []}
    summary = _sync_wallet_list(wallets, months)
    _sync_state["last_run"] = datetime.now(timezone.utc).isoformat()
    _sync_state["last_result"] = summary
    return summary


async def _sync_loop():
    log.info("[LOOP] sync queue start: tick=%ss batch=%d stale=%dmin "
             "months_back=%d", SYNC_TICK_SECONDS, SYNC_BATCH_SIZE,
             SYNC_INTERVAL_MIN, SYNC_MONTHS_BACK)
    last_prune = 0.0
    while True:
        try:
            if time.monotonic() - last_prune >= 3600:
                try:
                    pruned = await asyncio.to_thread(
                        get_db().prune_sync_log, SYNC_LOG_RETENTION_DAYS)
                    if pruned:
                        log.info("[LOOP] pruned %d old sync_log rows", pruned)
                except Exception as e:
                    log.warning("[LOOP] prune sync_log failed: %s", e)
                last_prune = time.monotonic()
            if _begin_sync():
                try:
                    res = await asyncio.to_thread(sync_due_batch)
                    if res.get("wallets"):
                        log.info("[SYNC] batch done: %s", res)
                except Exception as e:
                    log.warning("[SYNC] batch failed: %s", e)
                finally:
                    _end_sync()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[LOOP] unexpected: %s", e)
        await asyncio.sleep(SYNC_TICK_SECONDS)


def _safe_next(next_url: str) -> str:
    """Only allow local absolute paths as post-login redirect targets."""
    return next_url if (next_url.startswith("/")
                        and not next_url.startswith("//")) else "/"


# ============ pages ============
# Console pages require login (as documented in README). /profit/<addr> stays
# public so its link can be shared with external readers.
@app.get("/", response_class=HTMLResponse,
         dependencies=[Depends(require_login)])
def home(request: Request):
    db = get_db()
    month = _month_param(request)
    page = _page_param(request)
    per_page = 30

    total_rows = db.count_wallets(only_enabled=True)
    total_pages = max((total_rows + per_page - 1) // per_page, 1)
    page = min(page, total_pages)

    rows = [dict(s) for s in db.get_summaries_page(
        month, per_page, (page - 1) * per_page)]

    total = db.get_month_total(month)
    sync_stats = db.get_sync_stats()
    months = _target_months(6)
    context = _make_json_safe({
        "title": "地址盈利总览",
        "authed": is_authed(request),
        "rows": rows,
        "month": str(month),
        "total": float(total),
        "sync_stats": sync_stats,
        "months": months,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total_rows,
            "total_pages": total_pages,
        },
    })
    context["request"] = request
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/wallets", response_class=HTMLResponse,
         dependencies=[Depends(require_login)])
def wallets_page(request: Request):
    db = get_db()
    page = _page_param(request)
    per_page = 30

    total_wallets = db.count_wallets()
    total_pages = max((total_wallets + per_page - 1) // per_page, 1)
    page = min(page, total_pages)

    paginated_wallets = [dict(w) for w in db.get_wallets_page(
        per_page, (page - 1) * per_page)]

    context = _make_json_safe({
        "title": "地址管理",
        "authed": is_authed(request),
        "wallets": paginated_wallets,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total_wallets,
            "total_pages": total_pages,
        },
        "sync_state": _sync_state,
        "log": [dict(l) for l in db.get_sync_log(30)],
    })
    context["request"] = request
    return templates.TemplateResponse(request, "wallets.html", context)


@app.get("/wallet/{address}", response_class=HTMLResponse,
         dependencies=[Depends(require_login)])
def wallet_detail(request: Request, address: str):
    db = get_db()
    month = _month_param(request)
    days = [dict(d) for d in db.get_daily(address, month)]
    summary = db.get_month_summary(address, month)
    wallet = db.get_wallet(address) or {}
    context = _make_json_safe({
        "title": f"地址 {address[:8]}…",
        "authed": is_authed(request),
        "address": address,
        "label": wallet.get("label", ""),
        "days": days,
        "summary": summary,
        "month": month,
        "months": _target_months(6),
    })
    context["request"] = request
    return templates.TemplateResponse(request, "detail.html", context)


@app.get("/profit/{address}", response_class=HTMLResponse)
def profit_page(request: Request, address: str):
    """Standalone public profit page for one address (shareable, no login)."""
    db = get_db()
    month = _month_param(request)
    days = [dict(d) for d in db.get_daily(address, month)]
    summary = db.get_month_summary(address, month)
    wallet = db.get_wallet(address) or {}
    win_days = sum(1 for d in days if d["realized_profit"] >= 0)
    loss_days = len(days) - win_days
    if days:
        best = max(days, key=lambda d: d["realized_profit"])
        worst = min(days, key=lambda d: d["realized_profit"])
        best_day = {"pnl": best["realized_profit"],
                    "date": _ts_to_date(best["date"])}
        worst_day = {"pnl": worst["realized_profit"],
                     "date": _ts_to_date(worst["date"])}
    else:
        best_day = {"pnl": 0.0, "date": "-"}
        worst_day = {"pnl": 0.0, "date": "-"}
    context = _make_json_safe({
        "title": f"盈利 {address[:8]}…",
        "authed": is_authed(request),
        "address": address,
        "label": wallet.get("label", ""),
        "days": days,
        "summary": summary,
        "month": month,
        "months": _target_months(6),
        "win_days": win_days,
        "loss_days": loss_days,
        "best_day": best_day,
        "worst_day": worst_day,
        "chart_dates": [_ts_to_date(d["date"]) for d in days],
        "chart_pnls": [d["realized_profit"] for d in days],
    })
    context["request"] = request
    return templates.TemplateResponse(request, "profit.html", context)


@app.get("/config", response_class=HTMLResponse,
         dependencies=[Depends(require_login)])
def config_page(request: Request):
    context = _make_json_safe({
        "title": "配置",
        "interval": SYNC_INTERVAL_MIN,
        "months_back": SYNC_MONTHS_BACK,
        "batch_size": SYNC_BATCH_SIZE,
        "tick_seconds": SYNC_TICK_SECONDS,
    })
    context["request"] = request
    return templates.TemplateResponse(request, "config.html", context)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if is_authed(request):
        return RedirectResponse(url=_safe_next(next), status_code=302)
    context = _make_json_safe({
        "error": request.query_params.get("error"),
        "next": _safe_next(next),
    })
    context["request"] = request
    return templates.TemplateResponse(request, "login.html", context)


@app.post("/login")
def login_submit(request: Request, username: str = Form(...),
                 password: str = Form(...), next: str = Form(default="/")):
    target = _safe_next(next)
    if _login_blocked():
        context = _make_json_safe({
            "error": "尝试次数过多，请 5 分钟后再试",
            "next": target,
        })
        context["request"] = request
        return templates.TemplateResponse(request, "login.html", context)

    user_ok = secrets.compare_digest(username.encode("utf-8"),
                                     AUTH_USERNAME.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"),
                                     AUTH_PASSWORD.encode("utf-8"))
    if user_ok and pass_ok:
        request.session["user"] = AUTH_USERNAME
        _login_failures.clear()
        log.info("[AUTH] login ok user=%s", username)
        return RedirectResponse(url=target, status_code=302)

    _record_login_failure()
    client = request.client.host if request.client else "unknown"
    log.warning("[AUTH] login failed from %s", client)
    context = _make_json_safe({
        "error": "用户名或密码错误",
        "next": target,
    })
    context["request"] = request
    return templates.TemplateResponse(request, "login.html", context)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ============ internal API (console use) ============
@app.post("/api/wallets/import", dependencies=[Depends(require_login)])
async def api_import_wallets(request: Request):
    """Import wallets from JSON body: {"wallets": [{address, label?}]}."""
    try:
        body = await request.json()  # may be a list of wallets
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("wallets", body.get("addresses", []))
    else:
        return {"success": False, "message": "wallets must be a list"}
    if not isinstance(items, list):
        return {"success": False, "message": "wallets must be a list"}
    if len(items) > MAX_IMPORT_ITEMS:
        return {"success": False,
                "message": f"too many wallets (max {MAX_IMPORT_ITEMS})"}
    res = await asyncio.to_thread(get_db().import_wallets, items)
    log.info("[IMPORT] json import: %s", res)
    return {"success": True, **res}


@app.post("/api/wallets/import_file", dependencies=[Depends(require_login)])
async def api_import_file(file: UploadFile = File(...)):
    """Import wallets from uploaded JSON file (max 5 MB)."""
    try:
        import json as _json
        raw = await file.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            return {"success": False, "message": "file too large (max 5 MB)"}
        data = _json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"success": False, "message": f"invalid JSON file: {e}"}
    if isinstance(data, dict):
        # Accept {"wallets": [...]}, {"addresses": [...]}, or a single wallet object
        items = data.get("wallets", data.get("addresses", data))
        if isinstance(items, dict):
            items = [items]
    else:
        items = data
    if not isinstance(items, list):
        return {"success": False, "message": "file must contain a list"}
    if len(items) > MAX_IMPORT_ITEMS:
        return {"success": False,
                "message": f"too many wallets (max {MAX_IMPORT_ITEMS})"}
    res = await asyncio.to_thread(get_db().import_wallets, items)
    log.info("[IMPORT] file %s: %s", file.filename, res)
    return {"success": True, **res}


@app.post("/api/wallets/add", dependencies=[Depends(require_login)])
async def api_add_wallet(request: Request):
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    address = str(body.get("address", "")).strip()
    if not address:
        return {"success": False, "message": "address required"}
    if not is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}
    try:
        get_db().upsert_wallet(
            address,
            label=str(body.get("label", "")),
            enabled=bool(body.get("enabled", True)),
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True}


@app.post("/api/wallets/delete", dependencies=[Depends(require_login)])
async def api_del_wallet(request: Request):
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    n = await asyncio.to_thread(get_db().delete_wallet,
                                str(body.get("address", "")))
    return {"success": bool(n), "deleted": n}


@app.post("/api/wallets/toggle", dependencies=[Depends(require_login)])
async def api_toggle_wallet(request: Request):
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    get_db().set_wallet_enabled(str(body.get("address", "")),
                                bool(body.get("enabled", True)))
    return {"success": True}


@app.get("/api/config/sources", dependencies=[Depends(require_login)])
def api_get_sources():
    """Report the fixed data-source configuration (GMGN only for now)."""
    return {
        "success": True,
        "default_source": "gmgn",
        "enable_gmgn": True,
        "enable_debot": False,
        "debot_available": False,
    }


@app.get("/api/sync/status", dependencies=[Depends(require_login)])
def api_sync_status():
    db = get_db()
    return {"success": True, "running": _sync_state["running"],
            "started_at": _sync_state["started_at"],
            "progress": _sync_state["progress"],
            "last_run": _sync_state["last_run"],
            "last_result": _sync_state["last_result"],
            "recent_log": db.get_sync_log(10)}


@app.post("/api/sync/now", dependencies=[Depends(require_login)])
async def api_sync_now(request: Request):
    """Start a full background sync and return immediately.

    Poll GET /api/sync/status for progress; the scheduled queue pauses while
    a full run is in flight.
    """
    global _full_sync_task
    try:
        body = _body_dict(await request.json())
    except Exception:
        body = {}
    months = body.get("months") or _target_months()
    if (not isinstance(months, list) or not months
            or not all(is_valid_month(m) for m in months)):
        return {"success": False,
                "message": "months must be a list of YYYY-MM strings"}

    db = get_db()
    wallets = await asyncio.to_thread(db.list_wallets, True)
    if not _begin_sync():
        return {"success": False, "message": "sync already running"}

    async def runner():
        try:
            summary = await asyncio.to_thread(sync_all, months, wallets)
            log.info("[SYNC] full run done: %s", summary)
        except Exception as e:
            log.warning("[SYNC] full run failed: %s", e)
        finally:
            _end_sync()

    _full_sync_task = asyncio.create_task(runner())
    return {"success": True, "started": True, "wallets": len(wallets),
            "months": months}


@app.post("/api/sync/wallet", dependencies=[Depends(require_login)])
async def api_sync_wallet(request: Request):
    """单地址刷新: 拉该地址最近月份并入库 (GMGN 逐钱包实时查)."""
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    address = str(body.get("address", "")).strip()
    if not address:
        return {"success": False, "message": "address required"}
    if not is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}
    months = body.get("months") or _target_months()
    if (not isinstance(months, list) or not months
            or not all(is_valid_month(m) for m in months)):
        return {"success": False,
                "message": "months must be a list of YYYY-MM strings"}
    db = get_db()
    if not db.get_wallet(address):
        return {"success": False, "message": "wallet not tracked"}
    try:
        res = await asyncio.to_thread(sync_one_wallet, address, months)
    except Exception as e:
        log.warning("[SYNC-ONE] %s failed: %s", address[:10], e)
        return {"success": False, "message": str(e)}
    ok_months = [m for m in res["months"] if "error" not in m]
    if not ok_months:
        return {"success": False, "message": "sync failed",
                "results": res["months"]}
    latest = db.get_recent_daily(address, days=7)
    return {"success": True, "wallet": address,
            "total_days": res["total_days"],
            "months": [m["month"] for m in ok_months],
            "results": res["months"],
            "sync_stats": db.get_sync_stats(),
            "pnl_7d": sum(d["realized_profit"] for d in latest)}


# ============ public data API (external callers) ============
@app.post("/api/track")
async def api_track(request: Request):
    """Track a new address: add if missing, crawl profit, store to DB.

    Body: {"address": "<sol>", "label": "optional",
           "months": ["2026-10"] optional, default current+back}
    Returns sync result + shareable profit page url.
    No login required (for external callers).
    """
    global _track_inflight
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    address = str(body.get("address", "")).strip()
    label = str(body.get("label", "")).strip()
    months = body.get("months") or _target_months()
    if not address:
        return {"success": False, "message": "address required"}
    if not is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}
    if (not isinstance(months, list) or not months
            or not all(is_valid_month(m) for m in months)):
        return {"success": False,
                "message": "months must be a list of YYYY-MM strings"}

    db = get_db()
    try:
        existed = bool(db.get_wallet(address))
        # label='' / enabled=None keep whatever is already stored
        db.upsert_wallet(address, label=label)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    async with _track_lock:
        if _track_inflight >= TRACK_MAX_CONCURRENT:
            return {"success": False, "wallet": address,
                    "added": not existed,
                    "message": "server busy, retry later",
                    "retry_after_seconds": 30}
        _track_inflight += 1
    try:
        res = await asyncio.to_thread(sync_one_wallet, address, months)
    except Exception as e:
        log.warning("[TRACK] %s sync crashed: %s", address[:10], e)
        return {"success": False, "wallet": address, "added": not existed,
                "months": months,
                "message": f"wallet saved but sync failed: {e}",
                "profit_url": f"/profit/{address}"}
    finally:
        async with _track_lock:
            _track_inflight -= 1

    ok_months = [m for m in res["months"] if "error" not in m]
    if not ok_months:
        errs = "; ".join(f"{m['month']}: {m['error']}"
                         for m in res["months"])
        log.warning("[TRACK] %s all months failed: %s", address[:10], errs)
        return {"success": False, "wallet": address, "added": not existed,
                "months": months, "results": res["months"],
                "message": f"wallet saved but sync failed: {errs[:300]}",
                "profit_url": f"/profit/{address}"}

    first_ok = ok_months[0]["month"]
    summary = db.get_month_summary(address, first_ok)
    log.info("[TRACK] %s added=%s sync=%s", address[:10], not existed, res)
    return {"success": True, "wallet": address, "added": not existed,
            "months": months, "results": res["months"], "summary": summary,
            "profit_url": f"/profit/{address}?month={first_ok}"}


@app.get("/api/profit/{address}")
def api_profit(address: str, month: Optional[str] = None):
    """Daily profit rows of one wallet. month='YYYY-MM' optional.

    三态返回（HTTP 均为 200，用 status 字段区分）：
      ok              - 地址存在且有盈利数据，直接返回 summary + days
      pending         - 地址存在但要查的数据还没有，等后台同步完成后才有
      not_found       - 地址不在跟踪列表里，外部方需先调 POST /api/track 登记
      invalid_address - 地址格式非法或月份格式错误，无法处理
    """
    db = get_db()

    # 验证地址格式
    if not is_valid_sol_address(address):
        return {
            "success": False,
            "status": "invalid_address",
            "wallet": address,
            "message": "invalid solana address",
        }

    # 验证月份格式（如果传入）
    if month and not is_valid_month(month):
        return {
            "success": False,
            "status": "invalid_address",
            "wallet": address,
            "month": month,
            "message": "invalid month format, use YYYY-MM",
        }

    # 状态 3: 地址不存在 -> 明确告诉外部方该发哪条增加指令
    try:
        wallet = db.get_wallet(address)
        if not wallet:
            return {
                "success": False,
                "status": "not_found",
                "wallet": address,
                "message": "address not tracked yet, call POST /api/track to add it",
                "action": {
                    "method": "POST",
                    "endpoint": "/api/track",
                    "body": {"address": address},
                    "description": "登记该地址并立即抓取历史盈利数据",
                },
            }

        # 查询日常数据
        days = db.get_daily(address, month)
        for d in days:
            d.pop("synced_at", None)

        # 状态 2: 要查的数据还没有
        #   - 该月已同步成功但 0 交易 -> 是真实结果，继续往下走返回空 days
        #   - 该月还没同步 / 同步失败 -> 数据后面才会有
        if not days:
            if month:
                slog = db.get_last_sync_log(address, month)
                has_synced = bool(slog and slog.get("status") == "ok")
            else:
                # No month: synced-and-zero-trades is still a real result, so
                # also consult the durable sync state (not just daily rows).
                slog = db.get_last_sync_log(address)
                has_synced = (bool(slog and slog.get("status") == "ok")
                              or db.has_daily_data(address))
            if not has_synced:
                return {
                    "success": True,
                    "status": "pending",
                    "wallet": address,
                    "month": month,
                    "message": ("address is tracked but profit data is not "
                                "available yet, it will appear after the next sync"),
                    "retry_after_seconds": max(SYNC_INTERVAL_MIN, 5) * 60,
                    "summary": None,
                    "days": [],
                }

        # 计算汇总（不要重复查询 days）
        if month:
            summary = db.get_month_summary(address, month, days=days)
        else:
            summary = {"wallet": address, "days": len(days),
                       "realized_profit": sum(d["realized_profit"] for d in days)}

        # 状态 1: 有数据
        return {"success": True, "status": "ok", "wallet": address,
                "month": month, "summary": summary, "days": days}

    except Exception as e:
        log.error(f"Error in api_profit({address}, month={month}): {e}", exc_info=True)
        return {
            "success": False,
            "status": "invalid_address",
            "wallet": address,
            "message": f"internal error: {str(e)[:100]}",
        }


@app.get("/api/profit")
def api_profit_all(month: Optional[str] = None):
    """Month summary across all enabled wallets. month='YYYY-MM'."""
    if month and not is_valid_month(month):
        return {"success": False,
                "message": "invalid month format, use YYYY-MM"}
    db = get_db()
    month = month or _target_months(0)[0]
    rows = db.get_all_summaries(month)
    total = sum(r["realized_profit"] for r in rows)
    return {"success": True, "month": month, "total_pnl": total,
            "wallets": rows}


@app.post("/api/wallets")
async def api_add_wallet_external(request: Request):
    """Add a new address to track (external API, no login required).

    Body: {"address": "<sol>", "label": "optional"}
    Existing wallets keep their label when label is omitted and their
    enabled flag is untouched.
    """
    try:
        body = _body_dict(await request.json())
    except Exception:
        return {"success": False, "message": "body must be JSON"}

    address = str(body.get("address", "")).strip()
    label = str(body.get("label", "")).strip()

    if not address:
        return {"success": False, "message": "address required"}
    if not is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}

    db = get_db()
    try:
        db.upsert_wallet(address, label=label)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    wallet = db.get_wallet(address)
    log.info("[API] wallet added/updated: %s label=%s", address[:10], label)
    return {"success": True, "wallet": dict(wallet) if wallet else {}}


@app.get("/api/wallets")
def api_list_wallets():
    return {"success": True, "wallets": get_db().list_wallets()}


@app.get("/api/health")
def api_health():
    return {"success": True, "service": "gmgndaily"}


def run_server(host: Optional[str] = None, port: Optional[int] = None):
    host = host or os.getenv("HOST", "0.0.0.0")
    port = port or int(os.getenv("PORT", "8038"))
    print(f"gmgndaily: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8038")))
    a = p.parse_args()
    run_server(a.host, a.port)
