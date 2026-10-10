from contextlib import asynccontextmanager
from threading import RLock
import sqlite3
from hashlib import sha256

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .agent import run_agent
from .config import ROOT, Settings, load_settings
from .database import initialize_database, outbox_count
from .conversations import Conversations, confirmation, initialize_chats
from .execution import InvalidProposal
from .provider import GeminiProvider
from .schemas import DecisionInput, TaskInput
from .security import dashboard, SecurityViolation
from .metrics import security_summary


def create_app(settings: Settings | None = None, provider_factory=GeminiProvider) -> FastAPI:
    settings = settings or load_settings()
    chats = Conversations(settings.database)
    # This local demo uses one worker. Serialize mutations so double clicks and
    # simultaneous follow-ups cannot consume the same pending proposal twice.
    chat_lock = RLock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(settings.database)
        initialize_chats(settings.database)
        yield

    app = FastAPI(title="AI Assistant", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def fresh_application_state(request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/dashboard"} or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return page('index.html')

    @app.get('/dashboard')
    def dashboard_page():
        return page('dashboard.html')

    @app.get('/api/dashboard')
    def dashboard_data():
        try:
            return dashboard(settings.database)
        except (sqlite3.Error, ValueError, TypeError):
            raise HTTPException(503, 'Security records are unavailable. Please try again.') from None

    @app.get('/api/security-summary')
    def summary_data():
        try:
            return security_summary(settings.database)
        except sqlite3.Error:
            raise HTTPException(503, 'Approval security engine status is unavailable.') from None

    def page(filename):
        # A refreshed page must not combine an old card renderer with a new API.
        # Content hashes change automatically whenever an asset changes.
        html = (ROOT / "static" / filename).read_text(encoding="utf-8")
        for name in ("app.js", "style.css", "dashboard.js", "dashboard.css", "security-center.js"):
            version = sha256((ROOT / "static" / name).read_bytes()).hexdigest()[:16]
            html = html.replace(f'/static/{name}', f'/static/{name}?v={version}')
        return HTMLResponse(html)

    @app.get("/health")
    def health():
        return {"status": "ok", "phase": 2, "provider": "gemini",
                "configured": settings.configured, "outbox_count": outbox_count(settings.database)}

    @app.post("/api/run")
    def run(request: TaskInput):
        with chat_lock:
            return run_message(request)

    def run_message(request: TaskInput):
        identifier = request.conversation_id
        if identifier:
            try:
                conversation = chats.get(identifier)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from None
            decision = confirmation(request.task)
            pending = [p for m in conversation["messages"] for p in m["proposals"] if p["status"] == "proposed"]
            if decision and pending:
                chats.append(identifier, "user", request.task)
                reviewed = None
                if len(pending) == 1 and decision != "clarify":
                    reviewed, message = apply_decision(identifier, pending[0]["id"], decision)
                else:
                    message = ("Do you want to approve or reject the pending action? Please use the card or say your choice clearly."
                               if len(pending) == 1 else
                               "There are several pending actions. Please use Approve or Reject on the specific card."
                               if pending else "There isn't a pending action to review. What would you like me to prepare?")
                    chats.append(identifier, "assistant", message)
                return {"status": "completed", "message": message, "activity": [], "proposals": [],
                        "sent": bool(reviewed and reviewed["sent"]), "simulated": True,
                        "proposal": reviewed, **chats.get(identifier)}
        if not settings.configured:
            raise HTTPException(503, "The assistant isn't connected yet. Please configure the local API key and model, then restart the app.")
        identifier = identifier or chats.create()
        context = chats.context(identifier)
        chats.append(identifier, "user", request.task)
        provider = None
        try:
            provider = provider_factory(settings)
            if hasattr(provider, "set_context"):
                provider.set_context(context)
            result = run_agent(request.task, provider)
        except Exception:
            # Persist failure feedback too, so reloads and follow-ups stay coherent.
            chats.append(identifier, "assistant", "I couldn't complete that request. Please try again.")
            raise HTTPException(502, "I couldn't complete that request. Please check the connection and try again.") from None
        finally:
            if provider is not None:
                provider.close()
        chats.append(identifier, "assistant", result["message"], result["activity"], result["proposals"])
        return {**result, **chats.get(identifier)}

    @app.get("/api/conversations/{identifier}")
    def conversation(identifier: str):
        try:
            return chats.get(identifier)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/conversations")
    def new_conversation():
        return chats.get(chats.create())

    @app.post("/api/decisions")
    def decide(request: DecisionInput):
        with chat_lock:
            proposal, message = apply_decision(request.conversation_id, request.proposal_id, request.decision)
            return {"proposal": proposal, "message": message, "sent": proposal["sent"], "simulated": True,
                    **chats.get(request.conversation_id)}

    def apply_decision(identifier: str, proposal_id: str, decision: str):
        try:
            proposal, message = chats.decide(identifier, proposal_id, decision)
        except SecurityViolation as exc:
            raise HTTPException(403, str(exc)) from None
        except InvalidProposal as exc:
            raise HTTPException(422, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except sqlite3.Error:
            raise HTTPException(503, "The decision couldn't be saved. Please reload the conversation and try again.") from None
        if proposal["status"] in {"failed", "blocked"}:
            raise HTTPException(503, message)
        return proposal, message

    return app


app = create_app()
