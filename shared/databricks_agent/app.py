"""Databricks agent — a FastAPI Databricks App.

This is the *server* side of the distributed trace. The LangGraph orchestrator
calls POST /inquiry over HTTP, forwarding trace-context headers. We re-open that
context so every span this app produces nests under the caller's trace —
producing one unified trace across both services.

This single codebase supports two observability backends, chosen by the
TRACING_BACKEND env var:

  * "langsmith" — LangSmith (langsmith-trace + baggage headers);
                  hinge is `ls.tracing_context(parent=headers)`.
  * "mlflow"    — Databricks MLflow experiment (W3C traceparent header);
                  hinge is `mlflow.tracing.set_tracing_context_from_http_request_headers(headers)`.
  * "both"      — emit to LangSmith AND MLflow simultaneously (same flow, two panes).

The data lookup is a real Unity Catalog query against `<UC_CATALOG>.<UC_SCHEMA>`
(default `main.field_medical`) on a Databricks SQL warehouse. The LLM draft goes
through the Databricks Foundation Model API (OpenAI-compatible /serving-endpoints).
"""
from __future__ import annotations

import functools
import os
from contextlib import ExitStack, contextmanager

import langsmith as ls
import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem
from fastapi import FastAPI, Request
from langsmith.wrappers import wrap_openai
from mlflow.entities import SpanType
from openai import OpenAI
from pydantic import BaseModel

FMAPI_ENDPOINT = os.environ.get("FMAPI_ENDPOINT", "databricks-claude-sonnet-4-5")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")  # required; set via env / app resource
UC_CATALOG = os.environ.get("UC_CATALOG", "main")
UC_SCHEMA = os.environ.get("UC_SCHEMA", "field_medical")

# ─── Tracing backend selection ───────────────────────────────────────────────
TRACING_BACKEND = os.environ.get("TRACING_BACKEND", "langsmith").lower()
_USE_LANGSMITH = TRACING_BACKEND in ("langsmith", "both")
_USE_MLFLOW = TRACING_BACKEND in ("mlflow", "both")

# MLflow config (only used when _USE_MLFLOW). For distributed tracing on
# Databricks, spans merge across services ONLY when traces are stored in a
# Unity Catalog schema (the classic experiment artifact store does NOT merge
# cross-process spans). Both services must target the same experiment + UC
# schema. See scenario_2_mlflow/README.md.
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "databricks")
MLFLOW_EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT")  # workspace path, e.g. /Users/<you>/distributed-tracing
MLFLOW_UC_CATALOG = os.environ.get("MLFLOW_UC_CATALOG")  # a UC catalog you can create tables in
MLFLOW_UC_SCHEMA = os.environ.get("MLFLOW_UC_SCHEMA")    # e.g. mlflow_traces
MLFLOW_UC_TABLE_PREFIX = os.environ.get("MLFLOW_UC_TABLE_PREFIX", "dtdemo")


def _configure_mlflow() -> None:
    """Point MLflow at the shared Databricks experiment, backed by a UC schema.

    UC-backed trace storage is what lets this app's spans merge into the remote
    caller's trace. Requires MLFLOW_TRACING_SQL_WAREHOUSE_ID (or a default
    warehouse) for the UC tables.
    """
    from mlflow.entities.trace_location import UnityCatalog

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    trace_location = None
    if MLFLOW_UC_CATALOG and MLFLOW_UC_SCHEMA:
        trace_location = UnityCatalog(
            catalog_name=MLFLOW_UC_CATALOG,
            schema_name=MLFLOW_UC_SCHEMA,
            table_prefix=MLFLOW_UC_TABLE_PREFIX,
        )
    if MLFLOW_EXPERIMENT:
        # For an experiment already bound to a UC location, trace_location is
        # optional and ignored; passing it makes first-time creation explicit.
        mlflow.set_experiment(
            experiment_name=MLFLOW_EXPERIMENT, trace_location=trace_location
        )
    # Auto-trace OpenAI-compatible LLM calls into MLflow (analog of wrap_openai).
    mlflow.openai.autolog()


if _USE_MLFLOW:
    _configure_mlflow()

# Map our logical span kinds to each backend's span-type vocabulary.
_MLFLOW_SPAN_TYPES = {"retriever": SpanType.RETRIEVER, "chain": SpanType.CHAIN}


def traced(run_type: str, name: str):
    """Decorator that records a span in whichever backend(s) are active.

    Applies `@ls.traceable` and/or `@mlflow.trace` per TRACING_BACKEND, so the
    same function body produces nested spans in LangSmith, MLflow, or both.
    """

    def decorator(fn):
        wrapped = fn
        if _USE_LANGSMITH:
            wrapped = ls.traceable(run_type=run_type, name=name)(wrapped)
        if _USE_MLFLOW:
            span_type = _MLFLOW_SPAN_TYPES.get(run_type, SpanType.CHAIN)
            wrapped = mlflow.trace(name=name, span_type=span_type)(wrapped)
        return wrapped

    return decorator


@contextmanager
def incoming_trace_context(headers: dict[str, str]):
    """Continue the caller's trace from the propagated HTTP headers.

    Enters the LangSmith and/or MLflow "resume trace context" managers per
    TRACING_BACKEND. Spans opened inside nest under the remote caller's trace.

    Note: pass a plain dict, not the Starlette Headers object — LangSmith probes
    byte-keyed lookups that Starlette's Headers.get rejects.
    """
    with ExitStack() as stack:
        if _USE_LANGSMITH:
            stack.enter_context(ls.tracing_context(parent=headers))
        if _USE_MLFLOW:
            stack.enter_context(
                mlflow.tracing.set_tracing_context_from_http_request_headers(headers)
            )
        yield


@functools.lru_cache(maxsize=1)
def _workspace() -> WorkspaceClient:
    """Databricks workspace client.

    Uses DATABRICKS_CONFIG_PROFILE for local dev (OAuth auto-refresh); on a
    deployed App the runtime injects host + a token, so a bare WorkspaceClient()
    picks up ambient credentials.
    """
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


def _fmapi_client() -> OpenAI:
    """OpenAI-compatible client pointed at Databricks Foundation Model APIs.

    Built from the workspace config so it works both locally (profile / OAuth)
    and on a deployed App (injected service-principal credentials), independent
    of the installed databricks-sdk version.
    """
    cfg = _workspace().config
    headers = cfg.authenticate()  # {"Authorization": "Bearer <token>"}
    token = headers["Authorization"].split(" ", 1)[1]
    client = OpenAI(api_key=token, base_url=f"{cfg.host.rstrip('/')}/serving-endpoints")
    # LangSmith needs the client wrapped to emit LLM spans; MLflow captures them
    # via the global openai.autolog() patch, so no per-client wrapping there.
    if _USE_LANGSMITH:
        client = wrap_openai(client)
    return client


def _query(sql: str, params: dict[str, str]) -> list[list]:
    """Run a parameterized SQL statement on the warehouse; return data rows."""
    r = _workspace().statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql,
        parameters=[
            StatementParameterListItem(name=k, value=v) for k, v in params.items()
        ],
        wait_timeout="30s",
    )
    return (r.result.data_array if r.result else None) or []


@traced(run_type="retriever", name="uc_genie_lookup")
def uc_genie_lookup(product: str) -> dict:
    """Unity Catalog lookup for product + adverse-event data via a SQL warehouse.

    Traced as a retriever span so it renders as a data-retrieval step in the
    unified trace. Uses parameterized queries (:product) to avoid injection.
    """
    tbl = f"{UC_CATALOG}.{UC_SCHEMA}"
    prod_rows = _query(
        f"SELECT generic_name, drug_class, standard_dose, renal_guidance, "
        f"hepatic_guidance, pregnancy_guidance FROM {tbl}.products "
        f"WHERE lower(product_name) = lower(:product)",
        {"product": product},
    )
    if not prod_rows:
        return {}
    g, cls, dose, renal, hepatic, pregnancy = prod_rows[0]

    ae_rows = _query(
        f"SELECT adverse_event, frequency, seriousness FROM {tbl}.adverse_events "
        f"WHERE lower(product_name) = lower(:product) ORDER BY adverse_event",
        {"product": product},
    )
    return {
        "generic_name": g,
        "class": cls,
        "standard_dose": dose,
        "renal_guidance": renal,
        "hepatic_guidance": hepatic,
        "pregnancy_guidance": pregnancy,
        "known_adverse_events": [
            {"event": e, "frequency": f, "seriousness": s} for e, f, s in ae_rows
        ],
    }


@traced(run_type="chain", name="databricks_field_medical_agent")
def handle_inquiry(inquiry: str, product: str, adverse_events: list[str]) -> dict:
    """Core Databricks-side agent logic: look up data, then draft with FMAPI."""
    record = uc_genie_lookup(product)

    if not record:
        return {
            "product_found": False,
            "draft": (
                f"No medical-information record was found for '{product}'. "
                "Escalate to the medical information team for manual handling."
            ),
            "adverse_events_flagged": adverse_events,
        }

    ae_lines = ", ".join(
        f"{a['event']} ({a['frequency']}, {a['seriousness']})"
        for a in record["known_adverse_events"]
    )
    context = (
        f"Product: {record['generic_name']} ({record['class']})\n"
        f"Standard dosing: {record['standard_dose']}\n"
        f"Renal impairment guidance: {record['renal_guidance']}\n"
        f"Hepatic impairment guidance: {record['hepatic_guidance']}\n"
        f"Pregnancy guidance: {record['pregnancy_guidance']}\n"
        f"Known adverse events: {ae_lines}"
    )

    client = _fmapi_client()
    resp = client.chat.completions.create(
        model=FMAPI_ENDPOINT,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a pharmaceutical medical-information specialist. Using "
                    "ONLY the provided reference data, draft a factual, non-promotional "
                    "response to the healthcare professional's inquiry. Do not invent "
                    "dosing or safety information. If the inquiry mentions a possible "
                    "adverse event, acknowledge it and note it will be recorded for "
                    "pharmacovigilance. Keep it concise."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Reference data:\n{context}\n\n"
                    f"HCP inquiry:\n{inquiry}\n\n"
                    f"Adverse events flagged by triage: {adverse_events or 'none'}"
                ),
            },
        ],
    )

    return {
        "product_found": True,
        "draft": resp.choices[0].message.content,
        "reference_used": record,
        "adverse_events_flagged": adverse_events,
    }


# ─── HTTP surface ────────────────────────────────────────────────────────────
app = FastAPI(title="Databricks Field-Medical Agent")


class InquiryRequest(BaseModel):
    inquiry: str
    product: str
    adverse_events: list[str] = []


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "endpoint": FMAPI_ENDPOINT,
        "data_source": f"{UC_CATALOG}.{UC_SCHEMA}",
        "warehouse_id": WAREHOUSE_ID,
        "tracing_backend": TRACING_BACKEND,
    }


@app.post("/inquiry")
def inquiry(body: InquiryRequest, request: Request) -> dict:
    # A synchronous handler: FastAPI runs it in a worker thread, so the tracing
    # context manager and all spans live in one thread (MLflow/OpenTelemetry
    # span context is thread-local — an `async def` handler would hop threads
    # and lose it).
    #
    # THE distributed-tracing hinge: continue the caller's trace so every span
    # produced inside this block nests under the LangGraph agent's trace — in
    # whichever backend(s) TRACING_BACKEND selects.
    headers = dict(request.headers)
    with incoming_trace_context(headers):
        result = handle_inquiry(body.inquiry, body.product, body.adverse_events)

    # MLflow exports spans on a background queue that normally flushes at process
    # exit; a long-running server never exits, so flush per-request to make the
    # trace show up promptly. (No-op when MLflow isn't the active backend.)
    if _USE_MLFLOW:
        mlflow.flush_trace_async_logging()
    return result
