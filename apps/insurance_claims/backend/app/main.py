"""Backend HTTP API. Also serves the static frontend so the whole demo runs with one command.

Safe to host publicly:
  * a visitor's model / API key applies only to their own browser (X-Client-Id), never to other visitors;
  * the server's own key can be limited to a few models (SERVER_KEY_MODELS) and every visitor is rate-limited.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import llm as llm_registry
from app.config import settings
from app.data.repository import get_repo
from app.sop.controller import Controller
from app.sop.phases import SPECS, WORKFLOW
from app.sop.state import SessionState
from app.sop.suggestions import suggest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Insurance Claims SOP Agent", version="1.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])  # frontend may be hosted separately
controller = Controller()
SESSIONS: dict[str, SessionState] = {}
CLIENT_MODELS: dict[str, llm_registry.ClientModel] = {}   # per-browser model choice (keys kept in memory only)
MAX_SESSIONS, SESSION_TTL = 500, 6 * 3600


class NewSession(BaseModel):
    consent_scenario: str = "default"


class ChatIn(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=2000)


class ModelIn(BaseModel):
    provider: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    api_version: str = ""
    region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""


# ---------------------------------------------------------------- rate limiting (per visitor IP)
def _limits() -> dict[str, tuple[int, int]]:
    """RATE_LIMITS="chat=40/600,session=20/600,model=20/600" -> {bucket: (max requests, window seconds)}"""
    out = {}
    for part in os.getenv("RATE_LIMITS", "chat=40/600,session=20/600,model=20/600").split(","):
        if "=" in part and "/" in part:
            name, spec = part.split("=", 1)
            n, w = spec.split("/", 1)
            out[name.strip()] = (int(n), int(w))
    return out


LIMITS = _limits()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def _rate_limit(request: Request, bucket: str) -> None:
    if bucket not in LIMITS:
        return
    limit, window = LIMITS[bucket]
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    q, now = _hits[(ip, bucket)], time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, f"You're sending requests too quickly. Please wait {int(window - (now - q[0])) + 1}s.")
    q.append(now)


# ---------------------------------------------------------------- helpers
def _snapshot(s: SessionState) -> dict:
    """Session state for the UI, plus a claim summary built from the claims system (never from the model).
    The summary only exists once the caller is cleared (verified, and approved if a representative)."""
    snap = s.public()
    v = s.verification
    snap["suggestions"] = suggest(s, get_repo(), settings.demo_today)
    snap["claim"] = None
    if v.verified and v.consent_status in ("not_required", "approved") and s.active_case_id:
        repo = get_repo()
        case = repo.case(v.party_id, s.active_case_id)
        if case:
            f = repo.fact_sheet(case, settings.demo_today)
            amounts = {k: f["amounts"][k]["value"] for k in ("expected_reimbursement_amount", "net_pay") if k in f["amounts"]}
            snap["claim"] = {
                "case_id": f["case_id"], "claim_type": f["claim_type"], "status": f["status"],
                "filed_on": f["filed_on_pretty"], "reason": f.get("denial_reason"),
                "documents_needed": f.get("documents_needed", []),
                "appeal_deadline": f.get("appeal_deadline_pretty"), "days_left": f.get("days_until_appeal_deadline"),
                "deadline_passed": f.get("appeal_deadline_passed"),
                "expected_payment": amounts.get("expected_reimbursement_amount") if f["status"] != "denied" else None,
                "paid_so_far": amounts.get("net_pay") if f["status"] != "denied" else None,
            }
    return snap


def _gc() -> None:
    now = time.time()
    for sid in [sid for sid, s in SESSIONS.items() if now - s.created > SESSION_TTL]:
        SESSIONS.pop(sid, None)
    for cid in [cid for cid, m in CLIENT_MODELS.items() if now - m.created > SESSION_TTL]:
        CLIENT_MODELS.pop(cid, None)
    while len(SESSIONS) > MAX_SESSIONS:
        SESSIONS.pop(next(iter(SESSIONS)))


def _get(sid: str) -> SessionState:
    if sid not in SESSIONS:
        raise HTTPException(404, "Session not found. Start a new chat.")
    return SESSIONS[sid]


def _client_entry(client_id: str | None) -> llm_registry.ClientModel | None:
    return CLIENT_MODELS.get(client_id or "")


def _require_ui_config() -> None:
    if not settings.allow_ui_llm_config:
        raise HTTPException(403, "Model configuration from the UI is disabled on this server.")


def _cfg(body: ModelIn) -> llm_registry.LLMConfig:
    if body.provider not in llm_registry.PROVIDERS:
        raise HTTPException(400, f"Unknown provider {body.provider}")
    try:
        return llm_registry.with_current_secrets(llm_registry.LLMConfig(**body.model_dump(), source="ui").filled())
    except PermissionError as ex:
        raise HTTPException(403, str(ex)) from ex


# ---------------------------------------------------------------- model
@app.get("/api/health")
def health():
    return {"ok": True, "model": llm_registry.status(), "demo_today": settings.demo_today.isoformat()}


@app.get("/api/model")
def model_status(x_client_id: str | None = Header(default=None)):
    entry = _client_entry(x_client_id)
    server = llm_registry.status()
    return {**llm_registry.status(entry), "ui_config_allowed": settings.allow_ui_llm_config,
            "server_model": server["model"], "server_provider": server["provider"],
            "server_key_models": llm_registry.server_key_models(),
            "providers": llm_registry.PROVIDERS}


@app.post("/api/model/list")
def model_list(body: ModelIn, request: Request):
    """Models available to the given key, or (no key typed, same provider) to the server's key."""
    _require_ui_config()
    _rate_limit(request, "model")
    own_key = any(getattr(body, f) for f in llm_registry.SECRET_FIELDS)
    server = llm_registry.status()
    allowed = llm_registry.server_key_models()
    if not own_key and body.provider == server["provider"] and allowed:
        return {"models": allowed, "note": "shared demo key"}
    try:
        body.model = body.model or (allowed[0] if allowed and not own_key else "")
        return {"models": llm_registry.list_models(_cfg(body))}
    except HTTPException as ex:
        return {"models": [], "error": ex.detail}
    except Exception as ex:  # noqa: BLE001 - the UI falls back to the suggested list
        return {"models": [], "error": f"{type(ex).__name__}: {str(ex)[:200]}"}


@app.post("/api/model/test")
def model_test(body: ModelIn, request: Request):
    _require_ui_config()
    _rate_limit(request, "model")
    return llm_registry.test_config(_cfg(body))


@app.post("/api/model")
def model_set(body: ModelIn, request: Request, x_client_id: str | None = Header(default=None)):
    """Connect a model for THIS browser only. The key stays in server memory and is never returned."""
    _require_ui_config()
    _rate_limit(request, "model")
    if not x_client_id:
        raise HTTPException(400, "Missing X-Client-Id header.")
    cfg = _cfg(body)
    result = llm_registry.test_config(cfg)
    if not result["ok"] and cfg.provider != "rules":
        return {"connected": False, **result}
    entry = llm_registry.ClientModel(cfg, llm_registry.build(cfg))
    CLIENT_MODELS[x_client_id] = entry
    return {"connected": True, **result, **llm_registry.status(entry)}


@app.delete("/api/model")
def model_reset(x_client_id: str | None = Header(default=None)):
    """Go back to the server's default model."""
    CLIENT_MODELS.pop(x_client_id or "", None)
    return llm_registry.status()


# ---------------------------------------------------------------- conversation
@app.get("/api/sop")
def sop():
    return {"workflow": [p.value for p in WORKFLOW],
            "phases": {p.value: {"autonomy": s.autonomy.value, "goal": s.goal, "allowed_actions": s.allowed_actions,
                                 "claim_data_visible": s.claim_data_visible} for p, s in SPECS.items()}}


@app.get("/api/test-data")
def test_data():
    """Fixture identities, shown in the UI so testers know what to type (demo data only)."""
    repo = get_repo()
    return {"policyholders": repo.policyholders, "representatives": repo.representatives,
            "claims": [repo.brief(c) | {"party_id": c["party_id"]} for c in repo.claims],
            "consent_scenarios": list(repo.consent_scenarios)}


@app.post("/api/session")
def new_session(request: Request, body: NewSession | None = None):
    _rate_limit(request, "session")
    _gc()
    scenario = body.consent_scenario if body else "default"
    if scenario not in get_repo().consent_scenarios:
        raise HTTPException(400, f"Unknown consent scenario {scenario}")
    s = SessionState(consent_scenario=scenario)
    SESSIONS[s.session_id] = s
    greeting = controller.start(s)
    return {"session_id": s.session_id, "reply": greeting, "state": _snapshot(s)}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request, x_client_id: str | None = Header(default=None)):
    _rate_limit(request, "chat")
    entry = _client_entry(x_client_id)
    if not llm_registry.status(entry)["configured"]:
        raise HTTPException(409, "No model connected. Click 'Add your API key' to connect one, or choose rules-only.")
    s = _get(body.session_id)
    message = body.message.strip()
    for sug in suggest(s, get_repo(), settings.demo_today):   # remember which suggestion was picked, so it isn't offered again
        if message == sug["text"]:
            s.used_suggestions.add(sug["id"])
    token = llm_registry.use_client_model(entry)   # this visitor's model, for this request only
    try:
        with s.lock:
            out = controller.handle(s, body.message.strip())
    finally:
        llm_registry.reset_client_model(token)
    return {"reply": out["reply"], "explain": out["trace"]["explain"], "trace": out["trace"], "state": _snapshot(s)}


@app.get("/api/session/{sid}")
def get_session(sid: str):
    s = _get(sid)
    return {"state": _snapshot(s), "transcript": s.transcript, "traces": s.traces}


@app.get("/api/session/{sid}/outbox")
def outbox(sid: str):
    return {"emails": _get(sid).outbox}


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- frontend
@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Browsers must re-check the UI files on every load, so an updated build is never hidden by the cache."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


FRONTEND = settings.frontend_dir
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")
