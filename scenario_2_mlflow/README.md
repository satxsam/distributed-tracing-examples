# Scenario 2 — trace into a Databricks MLflow experiment

The same LangGraph → Databricks flow as [scenario 1](../scenario_1_langsmith/README.md),
but the unified trace lands in a **Databricks-managed MLflow experiment** instead of
LangSmith. Set `TRACING_BACKEND=mlflow` (or `both`).

> Read the [top-level README](../README.md) first for the shared mechanism and the
> network model. Agent code is in [`../shared/`](../shared/), used by both scenarios.

## The mechanism (mirror of scenario 1)

MLflow is OpenTelemetry-compatible and propagates trace context via the W3C
`traceparent` HTTP header:

```python
# client (orchestrator)
headers = mlflow.tracing.get_tracing_context_headers_for_http_request()  # {'traceparent': ...}
requests.post(AGENT_URL, json=payload, headers=headers)

# server (Databricks agent)
with mlflow.tracing.set_tracing_context_from_http_request_headers(dict(request.headers)):
    ...  # spans here nest under the caller's trace
```

`mlflow.openai.autolog()` captures the Foundation Model calls as spans (the MLflow
analog of LangSmith's `wrap_openai`).

## ⚠️ The one thing that will trip you up: traces must be stored in Unity Catalog

On Databricks-managed MLflow, **distributed tracing only merges cross-process spans
when traces are stored in a Unity Catalog schema.** The classic experiment artifact
store silently accepts the client's spans but drops the second service's — you get
two separate traces (or missing spans), not one.

So the experiment must be bound to a UC trace location:

```python
from mlflow.entities.trace_location import UnityCatalog

mlflow.set_experiment(
    experiment_name="/Users/you@example.com/distributed-tracing-uc",
    trace_location=UnityCatalog(
        catalog_name="your_catalog",
        schema_name="mlflow_traces",
        table_prefix="dtdemo",
    ),
)
```

and `MLFLOW_TRACING_SQL_WAREHOUSE_ID` must point at a warehouse you can use (the UC
trace tables — `<catalog>.<schema>.<prefix>_otel_spans` etc. — are created/queried
through it). Both services must target the **same experiment + UC schema**.

`shared/databricks_agent/app.py` and `shared/langgraph_agent/agent.py` do this
automatically from these env vars:

| Variable | Purpose |
|----------|---------|
| `MLFLOW_TRACKING_URI` | `databricks` |
| `MLFLOW_EXPERIMENT` | Workspace experiment path (shared by both services) |
| `MLFLOW_UC_CATALOG` / `MLFLOW_UC_SCHEMA` | UC schema for trace storage |
| `MLFLOW_UC_TABLE_PREFIX` | Table name prefix (default `dtdemo`) |
| `MLFLOW_TRACING_SQL_WAREHOUSE_ID` | Warehouse for the UC trace tables |

An experiment that **already contains traces** cannot be linked to a UC location —
use a fresh experiment name.

## Run it

### Option A: orchestrator local (or in a notebook) → deployed App

The orchestrator only makes outbound calls, so it runs anywhere. Point it at the
deployed Databricks App (see scenario 1 for how the App is deployed; the same App
serves both scenarios when deployed with `TRACING_BACKEND=both`).

```bash
set -a && source .env && set +a          # from repo root; .env has the MLFLOW_* vars
export TRACING_BACKEND=mlflow
export DATABRICKS_AGENT_URL=https://<your-app-url>/inquiry
export DATABRICKS_TOKEN=$(databricks auth token -p <profile> \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
cd shared && python -m langgraph_agent.run "How should Cardizafen be dosed in severe renal impairment?"
```

Then open the experiment in the workspace → **Traces**. One trace, spanning both
services:

```
field_medical_intake                     ← orchestrator (wherever it ran)
└─ call_databricks_agent
   └─ databricks_field_medical_agent      ← deployed Databricks App
      ├─ uc_genie_lookup                   ← Unity Catalog query
      └─ Completions                       ← Foundation Model call
```

### Option B: run it all inside Databricks (workshop)

See [`../workshop/`](../workshop/) — a notebook that runs the orchestrator in-notebook
against the deployed App, with no participant setup. The notebook is emphatic that the
orchestrator only runs there for convenience; it is not Databricks-specific.

## Deploying the App for MLflow tracing

Same as [scenario 1's deploy](../scenario_1_langsmith/README.md#run-it--option-b-deploy-the-databricks-agent-as-a-real-app),
plus:

- Set the MLflow env vars in `app.yaml` (already done; `TRACING_BACKEND=both`).
- Grant the App's **service principal**:
  - `CAN_MANAGE` on the MLflow experiment (it creates/writes traces there), and
  - `USE CATALOG` / `USE SCHEMA` / `CREATE TABLE` / `MODIFY` / `SELECT` on the UC
    trace schema.

```python
# grant experiment access to the app SP
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import ml
w = WorkspaceClient()
w.experiments.set_permissions(
    experiment_id="<exp_id>",
    access_control_list=[ml.ExperimentAccessControlRequest(
        service_principal_name="<app_sp_client_id>",
        permission_level=ml.ExperimentPermissionLevel.CAN_MANAGE)],
)
```

## Verifying the merge

```python
import mlflow
from mlflow import MlflowClient

tid = mlflow.get_last_active_trace_id()   # right after run_inquiry(...)
trace = MlflowClient().get_trace(tid)
assert any(s.name == "databricks_field_medical_agent" for s in trace.data.spans), \
    "server spans missing — check UC trace location + SQL warehouse"
print(len(trace.data.spans), "spans in one trace")
```

Or query the UC spans table directly:

```sql
SELECT trace_id, count(*) n, collect_set(name)
FROM your_catalog.mlflow_traces.dtdemo_otel_spans
GROUP BY trace_id ORDER BY max(start_time_unix_nano) DESC LIMIT 1;
```
