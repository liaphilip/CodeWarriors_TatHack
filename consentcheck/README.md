# AI Assistant demo

A local FastAPI chat application with real Gemini function calling, synthetic documents,
contextual follow-ups, proposal review, and simulated sends. **No real email is sent.**
Approving a pending proposal records a simulated send in the local SQLite outbox.

## Setup in VS Code (Windows PowerShell)

Open `C:\Users\annli\CodeWarriors_TatHack` in VS Code, then open a terminal at the repository root.
The existing Python 3.13 virtual environment is supported; activation is unnecessary.

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\consentcheck\requirements.txt
if (!(Test-Path .\consentcheck\.env)) { Copy-Item .\consentcheck\.env.example .\consentcheck\.env }
code .\consentcheck\.env
```

Set `GEMINI_API_KEY` and `GEMINI_MODEL` locally. The example file has placeholders only.
Choose a function-calling model available to your key from
[Google's model documentation](https://ai.google.dev/gemini-api/docs/models).
Never paste credentials into chat or frontend code. `.env` is Git-ignored and resolved
relative to the application directory. Existing environment variables take precedence.
Restart the backend after changing configuration. There is no fallback or fake LLM in
production; tasks and synthetic document results are sent to Gemini and may consume API quota.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir .\consentcheck --host 127.0.0.1 --port 8765 --reload
```

Open <http://127.0.0.1:8765>. Use one server worker for this local demo. Port 8000 may
already be occupied; leave unrelated servers untouched.

## Try the conversation

1. Click **Summarize a report**. The assistant searches and reads the synthetic quarterly report.
2. Ask **What was the profit in that report?** to exercise conversation context.
3. Ask **Prepare to send that report to Alice.** A card shows the actual document and recipient.
4. Select **Approve** or **Reject**, or reply **Yes, send it** / **No, cancel that**.
5. Refresh the page: history and reviewed-card states are restored.

Approval shows **Approved · simulated send completed** and a conversational confirmation
that no real email was sent. Rejection cancels the proposal without an outbox entry.
Ambiguous confirmations ask a clarifying question;
multiple pending actions require selecting a specific card. Reviewed proposals cannot be
submitted again. Read-only actions never get approval controls.

The composer supports Enter to send and Shift+Enter for a new line. Activity is presented
as short labels after the run finishes; a loading indicator appears while waiting. Responses
are not token-streamed. Basic bold text and bullet lists are rendered safely without injecting
model HTML. Raw tool JSON and model thought parts are not shown in the chat.

## Verify

```powershell
Push-Location .\consentcheck
..\.venv\Scripts\python.exe -m pytest -q
node --test tests/*.test.cjs
Pop-Location
Invoke-RestMethod http://127.0.0.1:8765/health
```

Tests use isolated databases and test-only provider responses; they need no API credentials
or paid calls. They cover tools, input validation, SDK function-response IDs and thought
signatures, context, persistence, ambiguous confirmations, duplicate and concurrent approvals,
outbox contents, failed-execution rollback, and additive database migration. The existing
Starlette test transport emits an httpx deprecation warning;
it does not fail the tests. The official Google Gen AI SDK is pinned to tested version 1.75.0.

## Implementation

- `app/main.py`: existing task/health endpoints, static files, and conversation/decision routes.
- `app/agent.py`: bounded, provider-independent tool loop and conversational results.
- `app/provider.py`: official Gemini SDK, manual tool calling, conversation context.
- `app/documents.py`, `schemas.py`, `tools.py`: synthetic dataset and validated tools.
- `app/conversations.py`: SQLite history and atomic proposal decisions/execution.
- `app/execution.py`: validates stored send details and inserts a simulated outbox record.
- `app/database.py`: outbox schema and additive migration, preserving existing rows.
- `static/`: responsive plain HTML, CSS, and JavaScript chat.
- `tests/`: offline regression coverage.

The same `search_documents`, `read_document`, and `propose_send_document` tools remain.
The agent must read a document during its current run before proposing to send it. Recipient
names are accepted; email addresses are never invented. There are no arbitrary shell,
filesystem, unrestricted network, or email tools.

SQLite stores conversations, messages, and proposals alongside the existing outbox in
`consentcheck/outbox.db`. Schema additions preserve existing data. The browser holds the
conversation ID in session storage; **New chat** clears that pointer without deleting history.
The last 20 messages, including document results and proposal states, are supplied as context.
Requests are serialized in this single-worker, local-only demo. It has no multi-user authentication,
conversation list, or cross-device synchronization.

Routes:

- `GET /health`: configuration and outbox status (never credentials).
- `POST /api/conversations`: create a chat.
- `GET /api/conversations/{id}`: retrieve messages and current proposal states.
- `POST /api/run`: `{task, conversation_id?}`; maintains the existing task endpoint.
- `POST /api/decisions`: `{conversation_id, proposal_id, decision}` where decision is `approve` or `reject`.

## Simulated execution

Both the Approve button and an unambiguous conversational approval use the same backend
decision method. It checks the proposal's conversation and pending state, validates the
synthetic document and recipient, and calls `simulate_send` within a SQLite transaction.
The outbox insert, final proposal state, and success message commit together. A write lock
and a unique outbox `proposal_id` prevent duplicate execution across separate connections.
The executor does not commit on its own and has no email client or external network access.

Outbox records include `proposal_id`, `document_id`, `document_title`, `document_content`,
`recipient`, `action_type` (`send_document`), `status` (`simulated_sent`), and `created_at`
(SQLite UTC timestamp). Successful proposals retain status `approved` with
`execution_status: completed`, `sent: true`, `simulated: true`, an outbox ID, and timestamp.
Here `sent: true` refers exclusively to a simulated local write, never real delivery.

If execution fails, its partial writes are rolled back and a `failed` proposal plus a
failure message are saved. The API returns an error; the UI reloads the failed card and
does not show a success confirmation. Prepare a new proposal to retry a failed execution.
If the database cannot save the overall transaction, the original proposal stays pending.
Rejected proposals remain unexecuted. Proposing an action alone still does not write to the outbox.

Migration runs on backend startup and only adds tables/columns/indexes. Existing outbox rows are
retained as `legacy` records. Earlier approvals without execution remain **Approved · not sent**
and cannot be replayed; create a new proposal to exercise simulated execution.

## Phase 2: action binding and dashboard

New proposals have a server-owned `secure_actions` record and a version-1 canonical action.
The descriptor explicitly includes tool, action type, recipient, CC/BCC, subject/body,
document ID/title/content SHA-256, conversation task ID, per-run ID, and local simulation
scope. Subject/body defaults are resolved once at proposal creation and displayed on the
card; empty CC/BCC lists are explicit. Deterministic sorted UTF-8 JSON is fingerprinted
using SHA-256. Run IDs, scope, fingerprints, and approval decisions never come from the model.

Approvals expire ten minutes after proposal creation. The single outbox writer runs the
mandatory gate: approved decision, expiry, one-time state, task/run/scope, candidate descriptor,
stored fingerprint, and current document content must match. Only trusted descriptor values
reach the outbox. Approval consumption, outbox insertion, and success evidence commit together.
Blocked decisions and execution failures are recorded in `security_events`; partial writes
roll back. No model tool or separate execution endpoint can bypass this path.

Existing rows are preserved without being retroactively certified. Legacy pending actions
must be prepared again; old completed sends remain visible without Phase 2 verification.

Open `/dashboard` using the navigation in chat. `/api/dashboard` is read-only and supplies
pending details/expiry, verification and execution state, recent audit events, and the actual
simulated outbox (most recent 200 records per section). Refresh or refocus the dashboard to
reload data. Approve/reject in chat; both pages use the same database. A current fingerprint
match is distinct from a recorded execution permission, and an approval alone is never shown
as a completed send.

This remains an unauthenticated, loopback-only demo. The trusted Python backend and SQLite
storage are the trust boundary: hashing does not protect against arbitrary database or code
modification, authenticate a user, or establish that the model chose the user's intended
recipient. Users must review the canonical card. No real email delivery is implemented. Live Gemini remains unchanged; tests use mocks.

## Troubleshooting

The PHANTOM LOOP main page uses equal desktop chat/security panels and a shared dark
theme with `/dashboard`. `/api/security-summary` aggregates the full secured history:
reviewed counts distinct actions with decisions or blocked events; executed requires
approved, executed state plus its matching simulated outbox row; blocked counts distinct
secured action IDs with blocked events; pending excludes decided and expired records.
Counts can overlap and exclude legacy/unknown action IDs. Metrics reload after chat actions,
on focus, on manual refresh, and every 30 seconds while visible. Unavailable responses clear
the counts rather than displaying fabricated zeros. The two scenario rows load persisted synthetic results; fetching or refreshing never runs an attack.

Missing configuration, authentication, model-access, quota, and connection failures produce
user-facing errors without raw SDK exception bodies. Check the local key/model and Google
quota, then retry. Runs are bounded to 8 model turns and 24 tool calls, with a 30-second
SDK timeout per model call. A multi-step task can take several minutes.

If the existing Python environment stops working, preserve it and create a separate one:

```powershell
py -3.13 -m venv .venv-consentcheck
.\.venv-consentcheck\Scripts\python.exe -m pip install -r .\consentcheck\requirements.txt
```

Use that interpreter for run/test commands and exclude its directory locally before staging.
Do not delete the old environment or Git history. SDK reference:
[Google Gen AI Python SDK](https://github.com/googleapis/python-genai).


## Phase 3: controlled synthetic demonstrations

The two buttons in ATTACK TYPES run Recipient Tampering and Misleading Approval Display.
Both are explicitly synthetic scenarios, not attacks on a live Gemini conversation.
The first changes Alice to Bob after approval; the second also changes the body while the
baseline's display still shows the original Alice/message approval. All addresses use
`example.test`. The deliberately vulnerable baseline is a pure comparison function with
no database, network, or executor access; it demonstrates acceptance, not a real send.

Each protected scenario creates a temporary SQLite database with the existing schema and
uses the real proposal registration, approval, and `simulate_send` security gate. Only
results go into the application's new additive `attack_runs` table. No demo proposal or
send enters the user's normal history, outbox, or four metrics. A malicious write is
measured before rollback, so rollback cannot masquerade as prevention. PREVENTED requires
an actual action-mismatch rejection, no unauthorized write, an unchanged approved descriptor,
and a successful legitimate control through the normal decision path. Accepted malicious
actions or writes are FAILED; other errors or incomplete evidence are INCOMPLETE.

Endpoints:
- `POST /api/security/attacks/recipient-tampering`
- `POST /api/security/attacks/misleading-approval`
- `GET /api/security/attacks` (counts and latest evidence for exactly two scenarios)

POST bodies accept only a 32-character hexadecimal `request_id`. SQLite uniqueness makes
retries/concurrent identical requests idempotent; the UI disables the active button and
retains its request ID after a lost response. Attempts are reserved before running; an
interrupted reservation remains explicitly INCOMPLETE, never counted as prevented. Once
an incomplete response is received, a new click starts a new attempt. The protected gate
is unchanged except for a specific mismatch exception subclass used to distinguish genuine
mismatch rejections from unrelated failures. There is no insecure assistant mode.
