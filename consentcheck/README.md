# ConsentCheck

ConsentCheck is a local security test bench for verifying that an AI agent action approved by a human is the same action that gets executed. The foundation uses a fixed demo proposal, server-owned approval records, and a SQLite outbox as a simulated side effect. Stage 1 adds four deterministic attack simulations against the same execution API. It sends no real email, reads no document files, and uses no AI, external APIs, cloud services, or authentication.

## Architecture

```text
Agent Proposal
      ↓
Human Approval
      ↓
Server Approval Record
      ↓
Secure Executor
      ↓
SQLite Outbox
      ↓
Execution Evidence
```

- `app.py`: HTTP routes, startup, static dashboard, and readable error responses.
- `schemas.py`: proposal validation, response models, and the default proposal.
- `approval_store.py`: server-generated IDs, timestamps, decisions, and expiry.
- `executor.py`: stored-value execution in one SQLite transaction.
- `database.py`: schema, connections, transactions, outbox access, and reset.
- `templates/index.html`, `static/style.css`, `static/app.js`: plain browser UI.
- `tests/`: database and real local HTTP tests using pytest and Python's standard library.

`mutations.py` contains pure request mutations; `attack_engine.py` creates approvals and inspects results entirely through the public HTTP API. It imports no database, approval-store, or executor functions. Later modules can build on these interfaces without replacing the foundation.

## Installation (Windows PowerShell, Python 3.11+)

From the repository root:

```powershell
cd consentcheck
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Running

From `consentcheck` with the virtual environment activated:

```powershell
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. API documentation is available at <http://127.0.0.1:8000/docs>.

If PowerShell blocks activation, no execution-policy change is needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

The development environment created for this checkout is at the repository root. To use that existing environment, run from the repository root:

```powershell
cd consentcheck
..\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

SQLite creates `consentcheck.db` alongside `app.py` at startup, regardless of the working directory. Records persist across server restarts. The server binds to loopback by default; this is an unauthenticated local demo, not a shared or internet-facing service. Stop it with Ctrl+C.

## Testing

From `consentcheck` with an activated environment:

```powershell
pytest -q
```

Without activation, using this checkout's root environment:

```powershell
..\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary SQLite databases and local ephemeral HTTP ports. They do not modify demo records. Coverage includes validation, server-owned metadata, successful execution, tampered execution payloads, expiry, rejection, duplicate execution, concurrent execution, database uniqueness, transactional rollback, missing approvals, static resources, and reset.

## Manual demo

1. Open the dashboard. Review the recipient, document name, and simulated content.
2. Click **Approve action**. The server generates an ID and records an APPROVED action with a 10-minute expiry.
3. Click **Execute approved action**. The record changes to EXECUTED, and the independently fetched outbox shows one SENT row. The execution button becomes disabled.
4. Click **Refresh evidence** to retrieve the persisted outbox again.
5. Click **Reset demo** to clear all approvals and outbox rows and restore the default proposal. This affects all tabs of this local demo.
6. Click **Reject action**. The server records REJECTED without ever granting approval; execution stays disabled. You may also reject after approval, before execution.
7. To demonstrate expiry, approve a fresh action and wait 10 minutes. Execution is blocked. API clients receive 403, and the server records EXPIRED when the approval is read or used.

A page reload starts a fresh proposal view and keeps the persisted outbox visible. The current decision ID is held only in the page, not restored from browser storage; existing records can still be retrieved by ID through the API.

## API

| Method | Path | Behavior |
|---|---|---|
| GET | `/` | Dashboard |
| POST | `/api/proposal` | Return the default validated proposal; no approval granted |
| POST | `/api/approvals` | Accept an ActionProposal and record APPROVED; return 201 |
| POST | `/api/proposal/reject` | Record a rejected proposal directly; return 201 |
| POST | `/api/approvals/{approval_id}/reject` | Reject an unexpired PENDING or APPROVED record |
| POST | `/api/execute/{approval_id}` | Execute stored values; optional `action` envelope must match approval |
| POST | `/api/attacks/{attack_type}` | Run one deterministic attack and return evidence with PASS/FAIL |
| GET | `/api/approvals/{approval_id}` | Get the stored approval; materialize expiry if due |
| GET | `/api/outbox` | Get persisted execution evidence, newest first |
| POST | `/api/reset` | Atomically clear both tables and return the default proposal |

Use the proposal returned by `/api/proposal` as the JSON body for `/api/approvals`. Client-supplied approval IDs, statuses, and timestamps are rejected with 422, as are unsupported tools. The only tool is `send_document`. Invalid state returns 403; duplicate execution or invalid rejection returns 409; unknown approval IDs return 404. SQLite failures return a readable 503 response with no stack trace.

PENDING is supported by the storage state model and cannot execute. The normal dashboard records a decision only when the user approves or rejects; it does not create a PENDING row just to display a proposal.

## Security principles

1. **Server owns approval records.** It generates secure random IDs and UTC creation and expiry timestamps.
2. **Client is untrusted.** Proposal inputs are validated; approval metadata cannot be supplied by the client. This MVP does not authenticate people or prove that a human clicked a button.
3. **Approved values are frozen.** There is no API for editing an approved action.
4. **Execution uses server-side approved values.** An optional action assertion is compared inside the execution transaction. Only stored recipient, document, and content can produce a side effect.
5. **Approval expires.** The server enforces a 10-minute lifetime even if browser controls are bypassed.
6. **Approval is single-use.** A status check plus `UNIQUE(outbox.approval_id)` prevents repeated execution.
7. **SQLite stores the simulated side effect.** `BEGIN IMMEDIATE` serializes verification, insertion, and status update. Failures roll back both writes. Rejection and reset use the same transaction discipline.
8. **No real email is sent.** SENT means only that a local outbox row exists. Document content is sample text; the PDF filename is a label.

All runtime assets are local. The frontend inserts API values using `textContent`, so stored content is not interpreted as HTML.

## Stage 1: Deterministic Attack Simulator

The **Attack Simulator** section has four test buttons:

| Attack type | Mutation | Expected rejection | Outbox for test approval |
|---|---|---|---|
| `recipient_substitution` | Change recipient to `attacker@example.com` | 403 mismatch | 0 → 0 |
| `content_substitution` | Change content, preserving task and document name | 403 mismatch | 0 → 0 |
| `replay` | Repeat a successfully executed request and approval ID | 409 already executed | 1 → 1 |
| `cross_task_reuse` | Claim a different task ID | 403 mismatch | 0 → 0 |

Each click creates a fresh legitimate proposal and approval through the existing endpoints. This is explicitly a synthetic test approval, not a human approval of a real operation. Replay then performs one legitimate setup execution. The engine sends a mutated request to the existing execution endpoint, reads `/api/outbox` before and after the attack, and filters evidence by its new approval ID. Other demo records are preserved. The dashboard shows the original action, mutated request, HTTP rejection, and full before/after evidence. Results remain visible until reset or page reload; approvals and outbox evidence persist in SQLite.

**PASS requires both the expected security rejection and unchanged outbox evidence.** An HTTP 500, 404, unrelated 403, missing evidence, or an unexpected write cannot count as PASS. Setup/transport/evidence failures return a structured 502 ERROR instead. Rejections are expected results and do not crash the server.

### Backward-compatible execution assertions

The original MVP intentionally ignored arbitrary execution request bodies. Its existing test verifies that a flat `{"recipient":"attacker@example.com"}` body executes only the original stored recipient. That behavior remains unchanged, as does bodyless execution.

Stage 1 adds an explicit envelope on the **same endpoint**, requiring all six action fields:

```json
{
  "action": {
    "actor_id": "demo_user",
    "task_id": "project_submission",
    "tool": "send_document",
    "recipient": "mentor@example.com",
    "document_name": "project_report.pdf",
    "document_content": "This is a simulated project report for the ConsentCheck MVP."
  }
}
```

Any differing assertion is rejected with 403 before any write; malformed envelopes or server metadata are rejected with 422. Matching assertions can execute normally. Comparison preserves exact input strings, including content whitespace. The executor still reads every execution value from the database. Omitting the envelope invokes the legacy behavior and **cannot substitute action values**. Thus simulator PASS means the explicit assertion was rejected and no attack-phase write occurred; it does not claim that legacy flat-body attempts are rejected.

The simulator reaches the running local HTTP server using its listening port, not the untrusted Host header. It disables proxies and redirects and needs no new dependencies. Run Uvicorn on the default IPv4 loopback address (`127.0.0.1`); IPv6-only serving is not supported by this local simulator transport.

### Verify the stage

1. Run `pytest -q` from the project directory with the environment activated.
2. Start the server and click all four attack buttons. Every card should show **PASS**, **BLOCKED**, and unauthorized side effect **NONE**.
3. Expect one new legitimate SENT record from replay setup; the three substitution/reuse attacks add no outbox rows.
4. Run normal **Approve action → Execute approved action**. It should add exactly one more legitimate row.
5. **Reset demo** clears approvals, outbox rows, and displayed attack results.

The original tests remain unchanged. `tests/test_attacks.py` additionally tests all four buttons' backend routes, exact-value matching (including filename, actor and whitespace), legitimate envelope execution, metadata rejection, isolated evidence, deterministic mutations, and result-classifier failures when execution writes despite a claimed rejection. No AI, adaptive learning, regression generation, or attack minimization is implemented.
