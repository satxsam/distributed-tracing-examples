# Cross-platform distributed tracing: LangGraph → Databricks → LangSmith

A minimal, working example of **one unified LangSmith trace that spans two
independently-hosted agents** — a LangGraph orchestrator and an agent running as
a Databricks App. The point is **interop and unified observability across a
service boundary**, not agent sophistication. The agents are deliberately
simple.

- A **LangGraph orchestrator** receives a question, does some reasoning, and
  calls a second agent over HTTP.
- A **Databricks agent** (a FastAPI [Databricks App](https://docs.databricks.com/dev-tools/databricks-apps/))
  queries Unity Catalog data and calls a Databricks Foundation Model to produce
  an answer.

Both trace to the **same LangSmith project**. Because the orchestrator
propagates its trace context across the HTTP call, everything the Databricks
agent does — its UC query, its LLM call — shows up as **child spans nested under
the same parent trace** in LangSmith, even though it ran on a different service.

```
   user question
        │
        ▼
┌──────────────────────────┐        LangSmith trace context
│  LangGraph orchestrator  │        (langsmith-trace + baggage HTTP headers)
│   • triage node          │  ──────────────────────────────────────────┐
│   • call_databricks ─────┼── HTTP POST /inquiry ─────►                 │
│   • compose node         │                          ┌──────────────────▼───────┐
└──────────────────────────┘                          │  Databricks App          │
        │                                              │  (FastAPI agent)         │
        │                                              │   • tracing_context(     │
        ▼                                              │       parent=headers)    │
    final answer                                       │   • Unity Catalog query  │
                                                       │   • Foundation Model call│
                                                       └──────────────────────────┘

               ═══════════════ ONE trace in LangSmith ═══════════════
```

Resulting trace (verified end-to-end against a deployed App):

```
field_medical_intake                          [chain]   ← root, in the orchestrator
└─ LangGraph
   ├─ triage
   │  └─ ChatOpenAI                            [llm]     ← Foundation Model call (orchestrator side)
   ├─ call_databricks_agent                    [tool]    ← the HTTP call
   │  └─ databricks_field_medical_agent        [chain]   ← runs INSIDE the Databricks App
   │     ├─ uc_genie_lookup                     [retriever] ← real Unity Catalog / SQL query
   │     └─ ChatOpenAI                          [llm]     ← Foundation Model call (Databricks side)
   └─ compose
```

All spans share **one trace ID**. There is no shared process and no shared
memory between the two services — only the propagated headers.

---

## The mechanism (this is the whole point)

LangSmith distributed tracing works by propagating two HTTP headers:
`langsmith-trace` (the trace + parent-run pointer) and `baggage` (metadata).

**Client side (orchestrator)** — extract headers from the active run tree and
send them with the outbound request:

```python
from langsmith.run_helpers import get_current_run_tree

headers = {}
if run_tree := get_current_run_tree():
    headers.update(run_tree.to_headers())   # langsmith-trace + baggage
requests.post(DATABRICKS_AGENT_URL, json=payload, headers=headers)
```

**Server side (Databricks App)** — continue the trace under the propagated
parent:

```python
import langsmith as ls

@app.post("/inquiry")
async def inquiry(body: InquiryRequest, request: Request):
    # Pass a plain dict, not the framework's Headers object (see "Gotchas").
    with ls.tracing_context(parent=dict(request.headers)):   # ← nests under caller
        return handle_inquiry(...)
```

That's it. Everything the Databricks agent does inside that context manager —
the Unity Catalog lookup, the Foundation Model call — becomes a child of the
orchestrator's trace. Wrapping each OpenAI-compatible client with LangSmith's
`wrap_openai()` makes the LLM calls appear as spans (with token counts) on both
sides.

---

## The example scenario

A pharmaceutical **field-medical information** flow (a generic, non-proprietary
enterprise use case):

1. A healthcare professional submits a medical-information inquiry (e.g. "dosing
   in severe renal impairment; patient reported palpitations").
2. The **LangGraph orchestrator** classifies the inquiry — extracts the product
   and flags any reported symptoms as potential adverse events
   (pharmacovigilance) — using a Databricks Foundation Model.
3. It delegates to the **Databricks agent**, which queries a Unity Catalog
   product + adverse-event reference store and uses a Foundation Model to draft a
   factual, non-promotional response.
4. The orchestrator wraps the draft with a standard medical-information
   disclaimer and returns it.

The reference data (three fictional drugs, `Cardizafen` / `Neuroliximab` /
`Glucoravir`, and their adverse events) is entirely made up — see
[`../shared/databricks_agent/seed_data.sql`](../shared/databricks_agent/seed_data.sql).

> New here? Read the [top-level README](../README.md) first — it covers the
> shared mechanism, the network model, and how the two scenarios relate. The
> agent code lives in [`../shared/`](../shared/) and is used by both scenarios.

---

## LLM backend

Both agents call **Databricks Foundation Model APIs** (the OpenAI-compatible
`/serving-endpoints` surface). You only need Databricks credentials and a
LangSmith API key — **no OpenAI key**. The default model endpoint is
`databricks-claude-sonnet-4-5`; override with `FMAPI_ENDPOINT`.

---

## Prerequisites

- Python 3.10+
- A **Databricks workspace** with:
  - the Databricks CLI installed and authenticated (`databricks auth login`, or a
    profile in `~/.databrickscfg`)
  - a **SQL warehouse** you can query
  - Foundation Model APIs enabled (pay-per-token or a provisioned endpoint)
- A **LangSmith account** and API key (`lsv2_...`).

---

## Setup

Run from the **repo root** (`distributed-tracing-examples/`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r shared/databricks_agent/requirements.txt

cp .env.example .env
# edit .env — see the variable reference below
```

### Seed the Unity Catalog data

Run [`../shared/databricks_agent/seed_data.sql`](../shared/databricks_agent/seed_data.sql)
on your SQL warehouse (SQL editor, or `databricks` CLI). It creates and populates
`main.field_medical.products` and `main.field_medical.adverse_events`. Change the
catalog/schema in the script (and in `UC_CATALOG` / `UC_SCHEMA`) if you don't
want to use the `main` catalog.

### Environment variables (`.env`)

| Variable | Purpose |
|----------|---------|
| `LANGSMITH_API_KEY` | Your LangSmith key. **Both** agents use the same one. |
| `LANGSMITH_PROJECT` | Project both agents trace into (any name). |
| `LANGSMITH_TRACING` | `true` to enable tracing. |
| `DATABRICKS_CONFIG_PROFILE` | CLI profile for local auth (OAuth auto-refresh). Alternatively set `DATABRICKS_HOST` + `DATABRICKS_TOKEN`. |
| `FMAPI_ENDPOINT` | Foundation Model serving endpoint name. |
| `DATABRICKS_WAREHOUSE_ID` | SQL warehouse the Databricks agent queries. |
| `UC_CATALOG` / `UC_SCHEMA` | Where the reference tables live (default `main` / `field_medical`). |
| `DATABRICKS_AGENT_URL` | Where the orchestrator sends the HTTP call. Local: `http://localhost:8010/inquiry`. Deployed: `https://<app-url>/inquiry`. |

---

## Run it — Option A: fully local (fastest)

Run the Databricks agent locally as a FastAPI service and fire an inquiry
through the orchestrator. Both processes talk to real Databricks FMAPI + SQL and
trace to real LangSmith — only the HTTP boundary is on localhost.

The agent modules live in `shared/`, so run these from the `shared/` directory
(with the repo-root `.env` loaded):

```bash
# terminal 1 — the Databricks agent
set -a && source .env && set +a
cd shared && uvicorn databricks_agent.app:app --port 8010

# terminal 2 — the orchestrator
set -a && source .env && set +a
export DATABRICKS_AGENT_URL=http://localhost:8010/inquiry
cd shared && python -m langgraph_agent.run "What is the recommended dosing of Cardizafen in patients with severe renal impairment? A patient also reported palpitations."
```

The script prints the final answer and a **LangSmith trace URL**. Open it: you'll
see the orchestrator's nodes and, nested under `call_databricks_agent`, the
spans that executed inside the local FastAPI service.

---

## Run it — Option B: deploy the Databricks agent as a real App

This puts a genuine network + service boundary between the two agents. The trace
is still unified.

```bash
# 1. Create the app (provisions its service principal). Note the SP client id it prints.
databricks apps create field-medical-agent

# 2. Store the LangSmith key as a secret and grant the app's SP read access.
databricks secrets create-scope field-medical-agent
databricks secrets put-secret field-medical-agent langsmith-api-key \
  --string-value "$LANGSMITH_API_KEY"
databricks secrets put-acl field-medical-agent <APP_SP_CLIENT_ID> READ

# 3. Grant the app's SP access to the data (run on your warehouse):
#      GRANT USE CATALOG ON CATALOG main                        TO `<APP_SP_CLIENT_ID>`;
#      GRANT USE SCHEMA  ON SCHEMA  main.field_medical          TO `<APP_SP_CLIENT_ID>`;
#      GRANT SELECT      ON TABLE   main.field_medical.products TO `<APP_SP_CLIENT_ID>`;
#      GRANT SELECT      ON TABLE   main.field_medical.adverse_events TO `<APP_SP_CLIENT_ID>`;

# 4. Attach resources so app.yaml's `valueFrom` references resolve.
#    Edit shared/databricks_agent/app_resources.json first: set your SQL warehouse id.
databricks apps update field-medical-agent --json @shared/databricks_agent/app_resources.json

# 5. Sync source + deploy (only the shared/databricks_agent dir is the app).
databricks sync ./shared/databricks_agent /Workspace/Users/<you>/apps/field-medical-agent
databricks apps deploy field-medical-agent \
  --source-code-path /Workspace/Users/<you>/apps/field-medical-agent
```

On the deployed App, auth is automatic: the Apps runtime injects the service
principal's OAuth credentials, which a bare `WorkspaceClient()` / `Config()`
picks up for both the UC query and the FMAPI call. The LangSmith key is read from
the secret via `valueFrom` in `app.yaml` — it is never baked into the image.

### Drive the deployed App from the local orchestrator

Databricks Apps require an authenticated caller, so the orchestrator forwards a
bearer token for non-localhost URLs:

```bash
set -a && source .env && set +a
export DATABRICKS_AGENT_URL=https://<your-app-url>/inquiry
export DATABRICKS_TOKEN=$(databricks auth token -p <profile> \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
cd shared && python -m langgraph_agent.run "What adverse events are associated with Cardizafen, and can it be used in severe renal impairment?"
```

The resulting LangSmith trace is unified across the real network boundary: the
App's `uc_genie_lookup` (SQL warehouse) and its `ChatOpenAI` span nest under the
orchestrator's `call_databricks_agent` node, all under one trace ID.

---

## Verifying the trace is really unified

In the LangSmith run, confirm:

- The root run is the orchestrator invocation (`field_medical_intake`).
- `call_databricks_agent` has children that executed **inside the Databricks
  agent**: `databricks_field_medical_agent`, `uc_genie_lookup`, and the
  Databricks-side `ChatOpenAI` LLM span.
- The trace ID is identical across both services.

You can also assert this programmatically with the LangSmith SDK by listing the
runs for the root `trace_id` and checking they all share it.

---

## Gotchas (learned deploying this)

Two fixes matter specifically on the Databricks Apps runtime; both are baked
into `app.py`:

1. **Pass `dict(request.headers)` to `tracing_context`, not the framework's
   `Headers` object.** LangSmith probes byte-keyed header lookups that Starlette's
   `Headers.get` rejects, raising `AttributeError: 'bytes' object has no attribute
   'encode'`. Converting to a plain dict is version-independent.
2. **Build the FMAPI client from `Config().authenticate()`**, not
   `serving_endpoints.get_open_ai_client()`. The latter is absent in the App
   runtime's pinned `databricks-sdk`. Deriving the bearer token from the config
   works both locally and on the App, across SDK versions.

---

## How it maps to your own use case

- Swap the scenario: the pattern (`get_current_run_tree().to_headers()` →
  HTTP → `tracing_context(parent=...)`) is domain-agnostic. Replace the triage
  prompt and the UC query with your own.
- Swap the data layer: `uc_genie_lookup` is a plain parameterized SQL query. Point
  it at your tables, or replace it with a Genie Conversation API call, a vector
  search, etc. — it stays a nested span either way.
- Add more hops: any downstream service that also uses LangSmith can continue the
  same trace by forwarding the headers again.

---

## Follow-on (out of scope here)

The reverse direction — a Databricks-hosted agent tracing to an **MLflow
experiment** on Databricks, with an external caller's context propagated in — is
a planned follow-up.
