# Databricks notebook source
# MAGIC %md
# MAGIC # Distributed tracing across a LangGraph ↔ Databricks boundary
# MAGIC
# MAGIC You'll run **two agents that trace as one** and watch a single unified trace span
# MAGIC both — first in **Databricks MLflow**, then (optionally) in **LangSmith** too, from
# MAGIC the *same* run.
# MAGIC
# MAGIC - A **LangGraph orchestrator** — receives a question, reasons, and calls a second
# MAGIC   agent over HTTP.
# MAGIC - A **Databricks agent** (FastAPI) — queries Unity Catalog data and calls a
# MAGIC   Foundation Model to answer.
# MAGIC
# MAGIC The orchestrator forwards its **trace context in the HTTP request headers**; the
# MAGIC agent re-opens that context, so the agent's spans nest under the orchestrator's
# MAGIC trace. Two processes, one trace.
# MAGIC
# MAGIC > ### The LangGraph orchestrator runs ANYWHERE
# MAGIC > It's plain Python — laptop, server, container, or this notebook are all fine. We
# MAGIC > run everything in this notebook **purely for workshop convenience** (no setup, a
# MAGIC > guaranteed runtime, ambient auth). Nothing here is Databricks-specific.
# MAGIC >
# MAGIC > ### Why a *local* agent in this workshop?
# MAGIC > We launch the Databricks agent as a background process **on this cluster driver**
# MAGIC > and call it over `localhost`. That's a real HTTP/trace boundary, it needs no App
# MAGIC > deployment, and — crucially for a shared workshop — it keeps **your** traces in
# MAGIC > **your** experiment. (A shared, pre-deployed App would funnel everyone's
# MAGIC > server-side spans into one experiment.) In production the agent would be a
# MAGIC > deployed App; see `scenario_2_mlflow/README.md`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module 0 — Setup
# MAGIC
# MAGIC Installs deps and **auto-namespaces every resource to you** (from your username), so
# MAGIC many people can run this in one workspace without colliding. You share only the SQL
# MAGIC warehouse and the read-only reference data.

# COMMAND ----------

# MAGIC %pip install -q -r ../requirements.txt
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ### 0.1 — Workshop settings
# MAGIC
# MAGIC These appear as **fillable fields at the top of the notebook** (Databricks widgets).
# MAGIC Your facilitator gives you the first three; the rest have sensible defaults. Fill
# MAGIC them in, then run the notebook top to bottom.

# COMMAND ----------

# Widgets = editable fields at the top of the notebook (no code editing needed).
# The reference data is seeded into your OWN schema (Module 0.3), so there's no
# shared data schema to configure — just a catalog you can create a schema in.
dbutils.widgets.text("workshop_catalog", "workshop", "Catalog (you can CREATE SCHEMA in)")
dbutils.widgets.text("sql_warehouse_id", "", "Shared SQL warehouse ID")
dbutils.widgets.text("fmapi_endpoint", "databricks-claude-sonnet-4-5", "Foundation Model endpoint")
dbutils.widgets.text("langsmith_api_key", "", "LangSmith API key (optional — blank = MLflow only)")

WORKSHOP_CATALOG = dbutils.widgets.get("workshop_catalog")
SQL_WAREHOUSE_ID = dbutils.widgets.get("sql_warehouse_id")
FMAPI_ENDPOINT = dbutils.widgets.get("fmapi_endpoint")
LANGSMITH_API_KEY = dbutils.widgets.get("langsmith_api_key")
AGENT_PORT = 8010  # local port for your agent (your own driver — no collision)

assert SQL_WAREHOUSE_ID, "Set the 'Shared SQL warehouse ID' widget (from your facilitator)."

# COMMAND ----------

# MAGIC %md
# MAGIC ### 0.2 — Derive your personal namespace and wire up the code

# COMMAND ----------

import os
import re
import sys

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()

# Point Python at the repo's shared/ agent code (this notebook is in <repo>/workshop/).
NOTEBOOK_PATH = ctx.notebookPath().get()
REPO_ROOT = os.path.abspath(os.path.join("/Workspace" + os.path.dirname(NOTEBOOK_PATH), ".."))
SHARED = os.path.join(REPO_ROOT, "shared")
assert os.path.isdir(SHARED), f"shared/ not found at {SHARED} — is the repo imported as a Git folder?"
sys.path.insert(0, SHARED)

# Everything below is keyed to YOU, so participants never collide.
USER = ctx.userName().get()                              # e.g. jane.doe@example.com
SLUG = re.sub(r"[^a-z0-9]+", "_", USER.split("@")[0].lower()).strip("_")  # jane_doe

MLFLOW_EXPERIMENT = f"/Users/{USER}/dtrace-workshop"     # user folder = already private to you
MLFLOW_UC_SCHEMA = f"dtrace_{SLUG}"                       # your own schema in the shared catalog
MLFLOW_UC_TABLE_PREFIX = "traces"
LANGSMITH_PROJECT = f"dtrace-{SLUG}"

# Your own copy of the reference data lives in the same personal schema — created
# in the next cell, so no shared/admin data setup is needed.
DATA_CATALOG = WORKSHOP_CATALOG
DATA_SCHEMA_NAME = MLFLOW_UC_SCHEMA

HAS_LANGSMITH = bool(LANGSMITH_API_KEY)
TRACING_BACKEND = "both" if HAS_LANGSMITH else "mlflow"

print(f"User:              {USER}")
print(f"Tracing backend:   {TRACING_BACKEND}")
print(f"MLflow experiment: {MLFLOW_EXPERIMENT}")
print(f"Your schema:       {WORKSHOP_CATALOG}.{MLFLOW_UC_SCHEMA}  (holds your data + traces)")
if HAS_LANGSMITH:
    print(f"LangSmith project: {LANGSMITH_PROJECT}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 0.3 — Create your private schema + seed your own sample data
# MAGIC
# MAGIC You get your own UC schema, which holds two things:
# MAGIC - The **sample reference data** the agent queries (`products`, `adverse_events`) —
# MAGIC   seeded here from `shared/sample_data.py`, so **no facilitator/admin data setup is
# MAGIC   required**. Everyone creates their own small copy.
# MAGIC - Your **MLflow trace tables** (created automatically when tracing starts). On
# MAGIC   Databricks, distributed traces merge across processes only when stored in a Unity
# MAGIC   Catalog schema — this is that schema, isolated to you.

# COMMAND ----------

import sample_data
from databricks.sdk import WorkspaceClient

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {WORKSHOP_CATALOG}.{MLFLOW_UC_SCHEMA} "
          f"COMMENT 'Distributed-tracing workshop — {USER}'")

# Seed YOUR copy of the reference data (idempotent) via the shared SQL warehouse.
sample_data.seed(
    DATA_CATALOG, DATA_SCHEMA_NAME,
    warehouse_id=SQL_WAREHOUSE_ID, workspace_client=WorkspaceClient(),
)
n_products = spark.table(f"{DATA_CATALOG}.{DATA_SCHEMA_NAME}.products").count()
n_events = spark.table(f"{DATA_CATALOG}.{DATA_SCHEMA_NAME}.adverse_events").count()
print(f"Ready: {WORKSHOP_CATALOG}.{MLFLOW_UC_SCHEMA} "
      f"({n_products} products, {n_events} adverse events)")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 0.4 — Launch YOUR Databricks agent (background process on this driver)
# MAGIC
# MAGIC This is the "server" side of the trace. It runs as a local FastAPI process here and
# MAGIC we'll call it over `localhost`. It's configured with your experiment + schema, so
# MAGIC the spans it records land in *your* trace.

# COMMAND ----------

import subprocess
import time
import urllib.request

# The agent process needs Databricks creds + the tracing config as env vars.
agent_env = dict(os.environ)
agent_env.update({
    "TRACING_BACKEND": TRACING_BACKEND,
    "FMAPI_ENDPOINT": FMAPI_ENDPOINT,
    "UC_CATALOG": DATA_CATALOG,
    "UC_SCHEMA": DATA_SCHEMA_NAME,
    "DATABRICKS_WAREHOUSE_ID": SQL_WAREHOUSE_ID,
    "MLFLOW_TRACKING_URI": "databricks",
    "MLFLOW_EXPERIMENT": MLFLOW_EXPERIMENT,
    "MLFLOW_UC_CATALOG": WORKSHOP_CATALOG,
    "MLFLOW_UC_SCHEMA": MLFLOW_UC_SCHEMA,
    "MLFLOW_UC_TABLE_PREFIX": MLFLOW_UC_TABLE_PREFIX,
    "MLFLOW_TRACING_SQL_WAREHOUSE_ID": SQL_WAREHOUSE_ID,
    # Ambient Databricks auth for a process on the driver:
    "DATABRICKS_HOST": "https://" + ctx.browserHostName().get(),
    "DATABRICKS_TOKEN": ctx.apiToken().get(),
})
if HAS_LANGSMITH:
    agent_env.update({
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": LANGSMITH_API_KEY,
        "LANGSMITH_PROJECT": LANGSMITH_PROJECT,
    })

_agent = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "databricks_agent.app:app",
     "--host", "127.0.0.1", "--port", str(AGENT_PORT), "--log-level", "warning"],
    cwd=SHARED, env=agent_env,
    stdout=open("/tmp/dtrace_agent.log", "w"), stderr=subprocess.STDOUT,
)

health = None
for _ in range(40):
    if _agent.poll() is not None:  # process died — surface its log immediately
        break
    try:
        health = urllib.request.urlopen(f"http://127.0.0.1:{AGENT_PORT}/health", timeout=2).read().decode()
        break
    except Exception:
        time.sleep(1)

if not health:
    print("⚠️ Agent did not come up. Last 40 lines of its log:\n")
    try:
        print("".join(open("/tmp/dtrace_agent.log").readlines()[-40:]))
    except Exception:
        print("(no log written)")
    raise RuntimeError(
        "Agent failed to start. If this is serverless and the error looks like a "
        "blocked subprocess or localhost bind, tell the facilitator — there is an "
        "in-process (background-thread) fallback that avoids the subprocess."
    )
print("Agent is up:", health)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module A — One unified trace in MLflow
# MAGIC
# MAGIC Run the orchestrator (here in the notebook — remember, it could run anywhere). It
# MAGIC calls your local agent over HTTP, forwarding the MLflow `traceparent` header. Both
# MAGIC sides' spans land in **one MLflow trace** in your experiment.

# COMMAND ----------

os.environ.update({
    "TRACING_BACKEND": TRACING_BACKEND,
    "FMAPI_ENDPOINT": FMAPI_ENDPOINT,
    "DATABRICKS_AGENT_URL": f"http://127.0.0.1:{AGENT_PORT}/inquiry",
    "MLFLOW_TRACKING_URI": "databricks",
    "MLFLOW_EXPERIMENT": MLFLOW_EXPERIMENT,
    "MLFLOW_UC_CATALOG": WORKSHOP_CATALOG,
    "MLFLOW_UC_SCHEMA": MLFLOW_UC_SCHEMA,
    "MLFLOW_UC_TABLE_PREFIX": MLFLOW_UC_TABLE_PREFIX,
    "MLFLOW_TRACING_SQL_WAREHOUSE_ID": SQL_WAREHOUSE_ID,
})
if HAS_LANGSMITH:
    os.environ.update({
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": LANGSMITH_API_KEY,
        "LANGSMITH_PROJECT": LANGSMITH_PROJECT,
    })

from langgraph_agent.agent import run_inquiry
import mlflow

result = run_inquiry(
    "What is the recommended dosing of Cardizafen in patients with severe renal "
    "impairment? A patient also reported palpitations after starting it."
)
print(result["final_answer"])
print("\nMLflow trace id:", mlflow.get_last_active_trace_id())

# COMMAND ----------

# MAGIC %md
# MAGIC **View it:** left nav → **Experiments** → open `dtrace-workshop` → **Traces**. The
# MAGIC one trace should look like:
# MAGIC ```
# MAGIC field_medical_intake                     ← orchestrator (this notebook)
# MAGIC └─ call_databricks_agent
# MAGIC    └─ databricks_field_medical_agent     ← your local agent process
# MAGIC       ├─ uc_genie_lookup                 ← Unity Catalog query
# MAGIC       └─ Completions                     ← Foundation Model call
# MAGIC ```
# MAGIC The next cell confirms the merge programmatically.

# COMMAND ----------

import mlflow
from mlflow import MlflowClient

tid = mlflow.get_last_active_trace_id()
mlflow.flush_trace_async_logging()
trace = MlflowClient().get_trace(tid)
by_id = {s.span_id: s for s in trace.data.spans}


def _depth(s):
    d, p = 0, s.parent_id
    while p and p in by_id:
        d, p = d + 1, by_id[p].parent_id
    return d


server_spans = {"databricks_field_medical_agent", "uc_genie_lookup"}
names = {s.name for s in trace.data.spans}
print(f"{len(trace.data.spans)} spans in ONE trace:")
for s in sorted(trace.data.spans, key=lambda s: s.start_time_ns):
    print(f"{'  ' * _depth(s)}- {s.name} [{s.span_type}]")
assert server_spans <= names, "server spans missing — is the agent using your UC schema?"
print("\n✅ Unified: the local agent's spans are nested in the orchestrator's trace.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module B — The same run, in LangSmith too (optional)
# MAGIC
# MAGIC If you set `LANGSMITH_API_KEY` in Module 0, `TRACING_BACKEND` is already `both`, so
# MAGIC **Module A's run was also recorded to LangSmith** — no extra work. Open your
# MAGIC LangSmith project (`dtrace-<your-username>`) and you'll find the same flow as a
# MAGIC unified trace there. One run, two observability backends.
# MAGIC
# MAGIC The cell below prints a direct link if LangSmith is enabled.

# COMMAND ----------

if HAS_LANGSMITH:
    from langsmith import Client

    c = Client(api_key=LANGSMITH_API_KEY)
    root = list(c.list_runs(project_name=LANGSMITH_PROJECT, is_root=True, limit=1))[0]
    allr = list(c.list_runs(project_name=LANGSMITH_PROJECT, trace_id=root.trace_id))
    print(f"LangSmith unified trace: {len(allr)} spans")
    print(root.url)
else:
    print("LangSmith not configured — set LANGSMITH_API_KEY in Module 0 to enable, then "
          "re-run Module 0.4 (relaunch agent) and Module A.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Module C — (Advanced, optional) the genuinely-reversed direction
# MAGIC
# MAGIC So far an **external** orchestrator called **into** the Databricks agent — which
# MAGIC needs no deployment of the orchestrator (it only makes outbound calls).
# MAGIC
# MAGIC To reverse it — a **Databricks**-hosted agent calling **out** to the LangGraph
# MAGIC agent — the network requirement flips: a callee must be reachable, and a laptop
# MAGIC (or a bare notebook) can't receive inbound calls. So the LangGraph agent must be
# MAGIC **deployed** somewhere with a URL, e.g. as its own Databricks App.
# MAGIC
# MAGIC Sketch (not run here):
# MAGIC 1. Wrap `langgraph_agent` in a FastAPI `POST /orchestrate` that calls `run_inquiry`
# MAGIC    inside `set_tracing_context_from_http_request_headers(headers)`.
# MAGIC 2. Deploy it as an App; grant the caller's service principal `CAN_USE` on it.
# MAGIC 3. From a Databricks-hosted caller, POST to it with
# MAGIC    `get_tracing_context_headers_for_http_request()` headers + a bearer token.
# MAGIC
# MAGIC The trace stitches the same way — only the direction of the HTTP call changes.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cleanup
# MAGIC
# MAGIC Stop your local agent process. Your trace data persists in your experiment and your
# MAGIC UC schema (`{WORKSHOP_CATALOG}.dtrace_<you>`), which the facilitator can drop later.

# COMMAND ----------

try:
    _agent.terminate()
    _agent.wait(timeout=10)
    print("Agent stopped.")
except Exception as e:
    print("Agent already stopped:", e)
