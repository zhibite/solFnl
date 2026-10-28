"""gmgndaily - debot sol wallet daily PnL crawler + web console.

Run:  cd app && python app.py   (or: uvicorn app:app --port 8038)
Env:  DEBOT_SESSION_ID (beegosessionID cookie, required for sync)
      DATABASE_PATH (default /data/gmgndaily.db)
      SYNC_INTERVAL_MIN (default 60), SYNC_MONTHS_BACK (default 2)
      GMGN_PASSWORD (web console login, default anywn076034)
"""

import asyncio
import logging
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from database import get_db
from debot_client import DebotClient, DebotError
from profit_source import ProfitSourceManager, SourceType

logging.basicConfig(stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("gmgndaily")

# ============ config ============
SESSION_ID = os.getenv("DEBOT_SESSION_ID", "")
SYNC_INTERVAL_MIN = int(os.getenv("SYNC_INTERVAL_MIN", "60"))
SYNC_MONTHS_BACK = int(os.getenv("SYNC_MONTHS_BACK", "2"))
CHAIN = os.getenv("CHAIN", "solana")

AUTH_USERNAME = "zengsw"
AUTH_PASSWORD = os.getenv("GMGN_PASSWORD", "anywn076034")
SESSION_SECRET = secrets.token_urlsafe(32)


def is_authed(request: Request) -> bool:
    return bool(request.session.get("user"))


_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _is_valid_sol_address(address: str) -> bool:
    return bool(_SOL_ADDR_RE.match(address))


class NeedLogin(Exception):
    def __init__(self, location: str):
        self.location = location


def require_login(request: Request):
    if not is_authed(request):
        nxt = request.url.path
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        raise NeedLogin(f"/login?next={nxt}")
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
    if _sync_task and not _sync_task.done():
        _sync_task.cancel()
        try:
            await _sync_task
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


def _ts_to_date(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)


templates.env.filters["timestamp_to_date"] = _ts_to_date


def _client() -> DebotClient:
    sid = SESSION_ID or str(get_db().get_config("debot_session_id", ""))
    if not sid:
        raise DebotError(-2, "DEBOT_SESSION_ID not configured; "
                             "set env or paste in /config page")
    return DebotClient(session_id=sid, chain=CHAIN)


def _source_manager() -> ProfitSourceManager:
    """Get configured source manager with current settings."""
    db = get_db()
    debot_sid = SESSION_ID or str(db.get_config("debot_session_id", "") or "")
    default_source = str(db.get_config("profit_source", "gmgn"))
    enable_gmgn = bool(db.get_config("enable_gmgn", True))
    enable_debot = bool(db.get_config("enable_debot", True))
    
    return ProfitSourceManager(
        debot_session_id=debot_sid,
        default_source=default_source,  # type: ignore
        enable_gmgn=enable_gmgn,
        enable_debot=enable_debot,
        chain=CHAIN
    )


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


# ============ sync engine ============
_sync_state: Dict[str, Any] = {"running": False, "last_run": None,
                               "last_result": None}
_sync_task: Optional[asyncio.Task] = None


def sync_one_wallet(wallet: str, months: List[str]) -> Dict[str, Any]:
    """Blocking sync of one wallet over given months. Returns summary."""
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
            err_msg = str(e)
            if hasattr(e, 'code'):
                err_msg = f"code={e.code} {getattr(e, 'message', str(e))}"
            db.add_sync_log(wallet, month, 0, 0.0, "error", err_msg)
            per_month.append({"month": month, "error": err_msg})
            log.warning("[SYNC] %s/%s failed: %s", wallet[:8], month, err_msg)
    
    return {"wallet": wallet, "total_days": total_days, "months": per_month}


def sync_all(months: Optional[List[str]] = None) -> Dict[str, Any]:
    db = get_db()
    months = months or _target_months()
    wallets = db.list_wallets(only_enabled=True)
    results = []
    for w in wallets:
        try:
            results.append(sync_one_wallet(w["address"], months))
        except DebotError as e:
            results.append({"wallet": w["address"],
                            "error": f"code={e.code} {e.message}"})
            if e.code == 101:
                break
    _sync_state["last_run"] = datetime.now(timezone.utc).isoformat()
    _sync_state["last_result"] = results
    return {"months": months, "wallets": len(wallets), "results": results}


async def _sync_loop():
    log.info("[LOOP] sync loop start, interval=%dmin months_back=%d",
             SYNC_INTERVAL_MIN, SYNC_MONTHS_BACK)
    while True:
        try:
            if not _sync_state["running"]:
                _sync_state["running"] = True
                try:
                    res = await asyncio.to_thread(sync_all)
                    log.info("[SYNC] done: %s", res.get("wallets"))
                except Exception as e:
                    log.warning("[SYNC] failed: %s", e)
                finally:
                    _sync_state["running"] = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[LOOP] unexpected: %s", e)
        await asyncio.sleep(max(SYNC_INTERVAL_MIN, 5) * 60)


# ============ pages ============
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    db = get_db()
    wallets = [dict(w) for w in db.list_wallets()]
    month = request.query_params.get("month") or _target_months(0)[0]
    page = int(request.query_params.get("page", 1))
    per_page = 50
    
    summaries = [dict(s) for s in db.get_all_summaries(month)]
    by_addr = {str(w["address"]): dict(w) for w in wallets}
    rows = []
    for s in summaries:
        row_dict = dict(s)
        wallet_info = by_addr.get(row_dict.get("wallet", ""), {})
        row_dict["label"] = wallet_info.get("label", "")
        row_dict["enabled"] = wallet_info.get("enabled", 0)
        rows.append(row_dict)
    
    total_rows = len(rows)
    total_pages = (total_rows + per_page - 1) // per_page
    
    # 确保页数有效
    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_rows = rows[start_idx:end_idx]
    
    total = sum(float(r.get("realized_profit", 0)) for r in paginated_rows)
    months = _target_months(6)
    context = _make_json_safe({
        "title": "地址盈利总览",
        "authed": is_authed(request),
        "rows": paginated_rows,
        "month": str(month),
        "total": float(total),
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


@app.get("/wallets", response_class=HTMLResponse)
async def wallets_page(request: Request):
    db = get_db()
    page = int(request.query_params.get("page", 1))
    per_page = 50
    
    all_wallets = [dict(w) for w in db.list_wallets()]
    total_wallets = len(all_wallets)
    total_pages = (total_wallets + per_page - 1) // per_page
    
    # 确保页数有效
    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_wallets = all_wallets[start_idx:end_idx]
    
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


@app.get("/wallet/{address}", response_class=HTMLResponse)
async def wallet_detail(request: Request, address: str):
    db = get_db()
    month = request.query_params.get("month") or _target_months(0)[0]
    days = [dict(d) for d in db.get_daily(address, month)]
    summary = dict(db.get_month_summary(address, month)) if db.get_month_summary(address, month) else {}
    wallets = {str(w["address"]): w for w in db.list_wallets()}
    context = _make_json_safe({
        "title": f"地址 {address[:8]}…",
        "authed": is_authed(request),
        "address": address,
        "label": wallets.get(address, {}).get("label", ""),
        "days": days,
        "summary": summary,
        "month": month,
        "months": _target_months(6),
    })
    context["request"] = request
    return templates.TemplateResponse(request, "detail.html", context)


@app.get("/profit/{address}", response_class=HTMLResponse)
async def profit_page(request: Request, address: str):
    """Standalone public profit page for one address (shareable, no login)."""
    db = get_db()
    month = request.query_params.get("month") or _target_months(0)[0]
    days = [dict(d) for d in db.get_daily(address, month)]
    summary = dict(db.get_month_summary(address, month)) if db.get_month_summary(address, month) else {}
    wallets = {str(w["address"]): w for w in db.list_wallets()}
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
        "label": wallets.get(address, {}).get("label", ""),
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


@app.get("/config", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def config_page(request: Request):
    db = get_db()
    sid = SESSION_ID or str(db.get_config("debot_session_id", "") or "")
    masked = (sid[:6] + "…" + sid[-4:]) if sid else "(未配置)"
    context = _make_json_safe({
        "title": "配置",
        "session_masked": masked,
        "has_session": bool(sid),
        "interval": SYNC_INTERVAL_MIN,
        "months_back": SYNC_MONTHS_BACK,
    })
    context["request"] = request
    return templates.TemplateResponse(request, "config.html", context)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if is_authed(request):
        return RedirectResponse(url=next or "/", status_code=302)
    context = _make_json_safe({
        "error": request.query_params.get("error"),
        "next": next or "/",
    })
    context["request"] = request
    return templates.TemplateResponse(request, "login.html", context)


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...), next: str = Form(default="/")):
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        request.session["user"] = AUTH_USERNAME
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        log.info("[AUTH] login ok user=%s", username)
        return RedirectResponse(url=target, status_code=302)
    context = _make_json_safe({
        "error": "用户名或密码错误",
        "next": next or "/",
    })
    context["request"] = request
    return templates.TemplateResponse(request, "login.html", context)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ============ internal API (console use) ============
@app.post("/api/wallets/import", dependencies=[Depends(require_login)])
async def api_import_wallets(request: Request):
    """Import wallets from JSON body: {"wallets": [{address, label?}]}."""
    db = get_db()
    try:
        body = await request.json()
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    items = body.get("wallets", body if isinstance(body, list) else [])
    if not isinstance(items, list):
        return {"success": False, "message": "wallets must be a list"}
    res = db.import_wallets(items)
    log.info("[IMPORT] json import: %s", res)
    return {"success": True, **res}


@app.post("/api/wallets/import_file", dependencies=[Depends(require_login)])
async def api_import_file(file: UploadFile = File(...)):
    """Import wallets from uploaded JSON file."""
    db = get_db()
    try:
        import json as _json
        raw = await file.read()
        data = _json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"success": False, "message": f"invalid JSON file: {e}"}
    items = data.get("wallets", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {"success": False, "message": "file must contain a list"}
    res = db.import_wallets(items)
    log.info("[IMPORT] file %s: %s", file.filename, res)
    return {"success": True, **res}


@app.post("/api/wallets/add", dependencies=[Depends(require_login)])
async def api_add_wallet(request: Request):
    body = await request.json()
    db = get_db()
    try:
        db.upsert_wallet(str(body.get("address", "")),
                         label=str(body.get("label", "")),
                         enabled=bool(body.get("enabled", True)))
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True}


@app.post("/api/wallets/delete", dependencies=[Depends(require_login)])
async def api_del_wallet(request: Request):
    body = await request.json()
    n = get_db().delete_wallet(str(body.get("address", "")))
    return {"success": bool(n), "deleted": n}


@app.post("/api/wallets/toggle", dependencies=[Depends(require_login)])
async def api_toggle_wallet(request: Request):
    body = await request.json()
    get_db().set_wallet_enabled(str(body.get("address", "")),
                                bool(body.get("enabled", True)))
    return {"success": True}


@app.post("/api/config/session", dependencies=[Depends(require_login)])
async def api_set_session(request: Request):
    """Save beegosessionID into DB (used when env var not set)."""
    body = await request.json()
    sid = str(body.get("session_id", "")).strip()
    if not sid:
        return {"success": False, "message": "session_id empty"}
    get_db().set_config("debot_session_id", sid)
    # validate
    try:
        ok = DebotClient(session_id=sid, chain=CHAIN).check_session()
    except Exception as e:
        return {"success": True, "saved": True,
                "valid": False, "message": f"saved but check failed: {e}"}
    return {"success": True, "saved": True, "valid": ok}


@app.get("/api/config/sources")
async def api_get_sources(request: Request):
    """Get current profit source configuration."""
    db = get_db()
    return {
        "success": True,
        "default_source": str(db.get_config("profit_source", "gmgn")),
        "enable_gmgn": bool(db.get_config("enable_gmgn", True)),
        "enable_debot": bool(db.get_config("enable_debot", True)),
    }


@app.post("/api/config/sources", dependencies=[Depends(require_login)])
async def api_set_sources(request: Request):
    """Configure profit data sources."""
    body = await request.json()
    db = get_db()
    
    default_source = str(body.get("default_source", "gmgn")).lower()
    if default_source not in ("gmgn", "debot"):
        return {"success": False, "message": "invalid default_source"}
    
    enable_gmgn = bool(body.get("enable_gmgn", True))
    enable_debot = bool(body.get("enable_debot", True))
    
    if not enable_gmgn and not enable_debot:
        return {"success": False, 
                "message": "at least one data source must be enabled"}
    
    # Validate debot if it's enabled
    if enable_debot:
        sid = SESSION_ID or str(db.get_config("debot_session_id", "") or "")
        if not sid:
            return {"success": False,
                    "message": "debot enabled but DEBOT_SESSION_ID not configured"}
        try:
            mgr = ProfitSourceManager(debot_session_id=sid, chain=CHAIN)
            if not mgr._debot or not mgr._debot.check_available():
                return {"success": False,
                        "message": "debot session invalid"}
        except Exception as e:
            return {"success": False,
                    "message": f"debot check failed: {e}"}
    
    db.set_config("profit_source", default_source)
    db.set_config("enable_gmgn", enable_gmgn)
    db.set_config("enable_debot", enable_debot)
    
    log.info("[CONFIG] sources updated: default=%s gmgn=%s debot=%s",
             default_source, enable_gmgn, enable_debot)
    
    return {"success": True}


@app.get("/api/sync/status")
async def api_sync_status():
    db = get_db()
    return {"success": True, "running": _sync_state["running"],
            "last_run": _sync_state["last_run"],
            "last_result": _sync_state["last_result"],
            "recent_log": db.get_sync_log(10)}


@app.post("/api/sync/now", dependencies=[Depends(require_login)])
async def api_sync_now(request: Request):
    """Trigger a manual sync (months from query/body, default current+back)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    months = body.get("months") or _target_months()
    if _sync_state["running"]:
        return {"success": False, "message": "sync already running"}
    _sync_state["running"] = True
    try:
        res = await asyncio.to_thread(sync_all, months)
    finally:
        _sync_state["running"] = False
    return {"success": True, **res}


# ============ public data API (external callers) ============
@app.post("/api/track")
async def api_track(request: Request):
    """Track a new address: add if missing, crawl profit, store to DB.

    Body: {"address": "<sol>", "label": "optional",
           "months": ["2026-10"] optional, default current+back}
    Returns sync result + shareable profit page url.
    No login required (for external callers).
    """
    try:
        body = await request.json()
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    address = str(body.get("address", "")).strip()
    label = str(body.get("label", "")).strip()
    months = body.get("months") or _target_months()
    if not address:
        return {"success": False, "message": "address required"}
    if not _is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}
    if not isinstance(months, list) or not months:
        return {"success": False, "message": "months must be a non-empty list"}
    db = get_db()
    try:
        existed = bool(db.get_wallet(address))
        db.upsert_wallet(address, label=label)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    try:
        res = await asyncio.to_thread(sync_one_wallet, address, months)
    except DebotError as e:
        log.warning("[TRACK] %s sync failed: code=%s %s",
                    address[:10], e.code, e.message)
        return {"success": False, "wallet": address, "added": not existed,
                "message": f"wallet saved but sync failed: code={e.code} {e.message}",
                "profit_url": f"/profit/{address}"}
    except Exception as e:
        log.warning("[TRACK] %s sync failed: %s", address[:10], e)
        return {"success": False, "wallet": address, "added": not existed,
                "message": f"wallet saved but sync failed: {e}",
                "profit_url": f"/profit/{address}"}
    log.info("[TRACK] %s added=%s sync=%s", address[:10], not existed, res)
    summary = db.get_month_summary(address, months[0]) if months else {}
    return {"success": True, "wallet": address, "added": not existed,
            "months": res.get("months", []), "summary": summary,
            "profit_url": f"/profit/{address}?month={months[0]}"}


@app.get("/api/profit/{address}")
async def api_profit(address: str, month: Optional[str] = None):
    """Daily profit rows of one wallet. month='YYYY-MM' optional.

    Example: GET /api/profit/215nhc...?month=2026-09
    """
    db = get_db()
    days = db.get_daily(address, month)
    if month:
        summary = db.get_month_summary(address, month)
    else:
        summary = {"wallet": address, "days": len(days),
                   "realized_profit": sum(d["realized_profit"] for d in days)}
    # strip internal field
    for d in days:
        d.pop("synced_at", None)
    return {"success": True, "wallet": address, "month": month,
            "summary": summary, "days": days}


@app.get("/api/profit")
async def api_profit_all(month: Optional[str] = None):
    """Month summary across all enabled wallets. month='YYYY-MM'."""
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
    Returns wallet info if created/updated.
    """
    try:
        body = await request.json()
    except Exception:
        return {"success": False, "message": "body must be JSON"}
    
    address = str(body.get("address", "")).strip()
    label = str(body.get("label", "")).strip()
    
    if not address:
        return {"success": False, "message": "address required"}
    if not _is_valid_sol_address(address):
        return {"success": False, "message": "invalid solana address"}
    
    db = get_db()
    try:
        db.upsert_wallet(address, label=label, enabled=True)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    
    wallet = db.get_wallet(address)
    log.info("[API] wallet added/updated: %s label=%s", address[:10], label)
    return {"success": True, "wallet": dict(wallet)}


@app.get("/api/wallets")
async def api_list_wallets():
    return {"success": True, "wallets": get_db().list_wallets()}


@app.get("/api/health")
async def api_health():
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
