# Distributed tracing across a LangGraph ↔ Databricks boundary

Two working examples of **one unified trace that spans two independently-hosted
agents** — a LangGraph orchestrator and an agent running as a Databricks App —
observed in two different backends:

| Scenario | Trace backend | Direction | Docs |
|----------|---------------|-----------|------|
| **1. LangSmith** | LangSmith (SaaS) | external agent → Databricks agent | [`scenario_1_langsmith/`](scenario_1_langsmith/README.md) |
| **2. MLflow** | Databricks-managed MLflow experiment | external agent → Databricks agent | [`scenario_2_mlflow/`](scenario_2_mlflow/README.md) |

Both scenarios share **one agent codebase** (`shared/`). Which backend the trace
lands in is chosen by a single environment variable, `TRACING_BACKEND`
(`langsmith` | `mlflow` | `both`). `both` emits the same run to LangSmith **and**
an MLflow experiment simultaneously — the same flow, two observability panes.

The point of these examples is **interop and unified observability across a
service boundary**, not agent sophistication. The agents are deliberately simple.

---

## The one idea

Distributed tracing works by propagating **trace-context HTTP headers** from the
caller to the callee. The callee re-opens that context, so its spans nest under
the caller's trace — even though the two run in different processes, on different
hosts, possibly on different platforms.

```
   ┌──────────────────────┐   trace-context headers   ┌──────────────────────┐
   │  caller (client)     │ ────────────────────────► │  callee (server)     │
   │  starts the trace,   │      HTTP request          │  re-opens the trace, │
   │  puts context in     │                            │  its spans nest      │
   │  the request headers │                            │  under the caller's  │
   └──────────────────────┘                            └──────────────────────┘
```

- **LangSmith** uses `langsmith-trace` + `baggage` headers.
- **MLflow** is OpenTelemetry-compatible and uses the W3C `traceparent` header.

The API is a near-exact mirror between the two backends:

| Step | LangSmith | MLflow |
|------|-----------|--------|
| client: get headers | `get_current_run_tree().to_headers()` | `mlflow.tracing.get_tracing_context_headers_for_http_request()` |
| server: continue trace | `ls.tracing_context(parent=dict(headers))` | `mlflow.tracing.set_tracing_context_from_http_request_headers(dict(headers))` |
| auto-trace LLM calls | `wrap_openai(client)` | `mlflow.langchain.autolog()` / `mlflow.openai.autolog()` |

---

## The network model (read this before choosing a topology)

Trace context flows **client → server**, so **whoever is the server (the callee)
must be network-reachable from the client.** This — not the choice of backend —
is what decides whether a given agent needs to be deployed.

| Caller | Callee | Does the caller need deploying? | Does the callee need deploying? |
|--------|--------|-------------------------------|--------------------------------|
| LangGraph orchestrator | Databricks agent | **No** — only makes outbound calls (laptop / notebook fine) | **Yes** — needs a reachable URL (a Databricks App gives it one) |
| Databricks agent | LangGraph agent | needs a runtime, but only outbound | **Yes** — a laptop behind NAT can't receive; it must be deployed (e.g. as its own App) |

Both scenarios here use the **first row**: an external LangGraph orchestrator
calls a deployed Databricks agent. That's why the orchestrator can run anywhere
— your laptop, a server, or (for the workshop) a Databricks notebook — while the
Databricks agent is deployed as an App.

> **The LangGraph agent is not special to Databricks.** It is plain Python and
> runs anywhere. In the workshop we execute it inside a Databricks notebook only
> because that removes per-participant setup and guarantees a working runtime —
> nothing about the orchestrator depends on Databricks.

The genuinely-reversed direction (Databricks calling *out* to a deployed
LangGraph agent) is covered as an optional advanced module in the workshop.

---

## Layout

```
distributed-tracing-examples/
├── README.md                      # you are here — the shared concept + network model
├── .env.example                   # copy to .env and fill in
├── requirements.txt               # orchestrator + local dev deps
├── shared/
│   ├── langgraph_agent/           # the orchestrator — RUNS ANYWHERE (laptop/notebook/server)
│   │   ├── agent.py
│   │   └── run.py
│   └── databricks_agent/          # the Databricks App agent (TRACING_BACKEND toggle)
│       ├── app.py
│       ├── app.yaml
│       ├── app_resources.json
│       ├── seed_data.sql
│       └── requirements.txt
├── scenario_1_langsmith/README.md # trace out to LangSmith (laptop-first walkthrough)
├── scenario_2_mlflow/README.md    # trace into a Databricks MLflow experiment
└── workshop/                      # Databricks notebook that runs it all, no laptop setup
    └── distributed_tracing_workshop.py
```

---

## Quick start

### On Databricks (fastest — the workshop notebook)

Add this repo as a **Git folder** and run the notebook; no local setup needed.

1. In your Databricks workspace, left sidebar → **Workspace**.
2. Go to your home folder (**Home**, or **Users → your-email**).
3. **Create** (top-right) → **Git folder**.
   *(Older UIs: click the **Repos** icon → **Add Repo**.)*
4. **Git repository URL**:
   `https://github.com/satxsam/distributed-tracing-examples`
   Provider auto-detects as GitHub → **Create Git folder**. (Public repo — no token needed.)
5. Open `workshop/distributed_tracing_workshop` inside the folder, attach it to
   **Serverless** (or any cluster with internet egress), fill in the widget fields at
   the top, and run top to bottom.

See [`workshop/`](workshop/) for participant + facilitator details.

### Locally (laptop)

```bash
git clone https://github.com/satxsam/distributed-tracing-examples
cd distributed-tracing-examples
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r shared/databricks_agent/requirements.txt
cp .env.example .env   # then edit
```

Then pick a scenario:

- **New here / want the fastest win** → [`scenario_1_langsmith/`](scenario_1_langsmith/README.md)
  runs the whole thing from your laptop against a deployed App and shows a unified
  LangSmith trace.
- **Want the Databricks-native backend** → [`scenario_2_mlflow/`](scenario_2_mlflow/README.md)
  traces the same flow into a Databricks MLflow experiment.
- **Running a group workshop** → [`workshop/`](workshop/) — a self-documenting
  Databricks notebook where everything (orchestrator + a local agent) runs on the
  cluster, auto-namespaced per participant. Facilitator prep in
  [`workshop/FACILITATOR.md`](workshop/FACILITATOR.md).
