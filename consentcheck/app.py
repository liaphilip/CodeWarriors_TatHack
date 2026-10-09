"""ConsentCheck local application. Run: uvicorn app:app --reload"""

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from attack_engine import AttackRunError, local_api_client, run_attack
from approval_store import ApprovalError, get_approval, record_decision, reject_approval
from database import DEFAULT_DB_PATH, init_database, list_outbox, reset_database
from executor import execute_approval
from schemas import (ActionProposal, ApprovalDetail, DEMO_PROPOSAL, ExecutionEvidence,
                     ExecutionRequest, AttackResult, AttackType)

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def create_app(db_path=DEFAULT_DB_PATH):
    @asynccontextmanager
    async def lifespan(application):
        init_database(db_path)
        yield

    application = FastAPI(title="ConsentCheck", version="0.1.0", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.exception_handler(ApprovalError)
    async def approval_error(request: Request, error: ApprovalError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.message})

    @application.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error):
        logger.exception("SQLite operation failed", exc_info=error)
        return JSONResponse(status_code=503, content={"detail": "Local database unavailable. Please retry."})

    @application.exception_handler(AttackRunError)
    async def attack_error(request: Request, error: AttackRunError):
        return JSONResponse(status_code=502, content={"detail": str(error), "status": "ERROR"})

    @application.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / "templates" / "index.html")

    @application.post("/api/proposal", response_model=ActionProposal)
    def proposal():
        return DEMO_PROPOSAL

    @application.post("/api/approvals", response_model=ApprovalDetail, status_code=201)
    def approve(proposal: ActionProposal):
        return record_decision(proposal, db_path)

    @application.post("/api/proposal/reject", response_model=ApprovalDetail, status_code=201)
    def reject_proposal(proposal: ActionProposal):
        return record_decision(proposal, db_path, approved=False)

    @application.post("/api/approvals/{approval_id}/reject", response_model=ApprovalDetail)
    def reject(approval_id: str):
        return reject_approval(approval_id, db_path)

    @application.post("/api/execute/{approval_id}", response_model=ExecutionEvidence)
    async def execute(approval_id: str, request: Request):
        # Preserve the foundation's bodyless/legacy requests. The new explicit
        # envelope adds assertions; stored values still drive every side effect.
        claimed_action = None
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict) and "action" in body:
            try:
                claimed_action = ExecutionRequest.model_validate(body).action
            except ValidationError:
                return JSONResponse(status_code=422, content={
                    "detail": "Invalid execution action envelope; provide only a complete action."})
        return await asyncio.to_thread(execute_approval, approval_id, db_path,
                                       claimed_action=claimed_action)

    @application.post("/api/attacks/{attack_type}", response_model=AttackResult)
    async def attack(attack_type: AttackType, request: Request):
        # Use the actual listening socket, never a client-controlled Host header
        # or URL. The simulator can reach only this local application's API.
        server = request.scope.get("server")
        if not server:
            raise AttackRunError("Attack simulator requires a running local HTTP server.")
        return await asyncio.to_thread(run_attack, attack_type, local_api_client(server[1]))

    @application.get("/api/approvals/{approval_id}", response_model=ApprovalDetail)
    def approval(approval_id: str):
        return get_approval(approval_id, db_path)

    @application.get("/api/outbox", response_model=list[ExecutionEvidence])
    def outbox():
        return list_outbox(db_path)

    @application.post("/api/reset", response_model=ActionProposal)
    def reset():
        reset_database(db_path)
        return DEMO_PROPOSAL

    return application


app = create_app()
