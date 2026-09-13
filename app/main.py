"""FastAPI entrypoint: auth, static dashboard, API mount."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, security
from .api.routes import router as api_router
from .core import cards as cards_mod
from .core import events

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Deal Desk", version=config.VERSION, docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup():
    db.init_db()
    restored = cards_mod.refresh_statement_limits()
    for msg in restored:
        events.log(None, "cards", msg)
    events.log(None, "system", f"Deal Desk {config.VERSION} started")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# --------------------------- auth ---------------------------------------
class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    time.sleep(0.25)  # crude throttle on guessing
    if not security.verify_password(body.password):
        events.log(None, "auth", "failed login attempt", level="warn")
        raise HTTPException(401, "wrong password")
    token = security.make_session()
    response.set_cookie(security.COOKIE, token, httponly=True, samesite="lax",
                        secure=not config.DB_PATH.startswith(str(config.ROOT)),
                        max_age=config.SESSION_HOURS * 3600)
    events.log(None, "auth", "signed in")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(security.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    sess = security.read_session(request.cookies.get(security.COOKIE))
    return {"authenticated": bool(sess), "user": (sess or {}).get("u"),
            "auto_buy": bool(db.get_setting("auto_buy", config.AUTO_BUY)),
            "version": config.VERSION}


@app.get("/api/health")
def health():
    try:
        db.q1("SELECT 1")
        return {"ok": True, "version": config.VERSION, "db": config.DB_PATH}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


app.include_router(api_router, prefix="/api")


# --------------------------- pages --------------------------------------
PAGES = {"": "index.html", "console": "console.html", "deals": "console.html",
         "deal": "deal.html", "inventory": "inventory.html", "cards": "cards.html",
         "filters": "filters.html", "playbooks": "playbooks.html", "trust": "trust.html",
         "settings": "settings.html", "testbench": "testbench.html", "tasks": "tasks.html",
         "merchants": "merchants.html"}


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/{page:path}")
def page(page: str, request: Request):
    if page.startswith("api/"):
        raise HTTPException(404)
    asset = STATIC / page
    if page and asset.is_file():
        return FileResponse(asset)
    name = PAGES.get(page.strip("/").split("/")[0], None)
    if name is None:
        raise HTTPException(404, "no such page")
    if not security.read_session(request.cookies.get(security.COOKIE)):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / name)
