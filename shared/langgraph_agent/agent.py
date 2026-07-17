"""LangGraph orchestrator — the *client* side of the distributed trace.

────────────────────────────────────────────────────────────────────────────
THIS ORCHESTRATOR RUNS ANYWHERE. It is plain Python — a laptop, a server, a
container, or a Databricks notebook are all equally valid runtimes. It only
makes *outbound* HTTP calls, so it never needs to be network-reachable itself.
The workshop happens to run it inside a Databricks notebook purely for
convenience (zero per-participant setup); nothing here depends on Databricks.
────────────────────────────────────────────────────────────────────────────

A minimal three-node graph:

    triage  ->  call_databricks_agent  ->  compose

`triage` classifies the inquiry (which product, any adverse events) using a
Databricks Foundation Model. `call_databricks_agent` makes the HTTP call to the
Databricks agent, forwarding trace-context headers so the remote agent's spans
nest under this trace. `compose` wraps the returned draft with the required
medical-information disclaimer.

The whole thing runs under one trace; the remote call becomes a subtree. The
observability backend is selected by TRACING_BACKEND (langsmith | mlflow | both)
— see the top-level README for the mechanism.
"""
from __future__ import annotations

import json
import os
from typing import TypedDict

import requests
from langgraph.graph import END, START, StateGraph

FMAPI_ENDPOINT = os.environ.get("FMAPI_ENDPOINT", "databricks-claude-sonnet-4-5")
DATABRICKS_AGENT_URL = os.environ.get(
    "DATABRICKS_AGENT_URL", "http://localhost:8010/inquiry"
)

# ─── Tracing backend selection (mirror of the Databricks agent) ──────────────
TRACING_BACKEND = os.environ.get("TRACING_BACKEND", "langsmith").lower()
_USE_LANGSMITH = TRACING_BACKEND in ("langsmith", "both")
_USE_MLFLOW = TRACING_BACKEND in ("mlflow", "both")

if _USE_MLFLOW:
    import mlflow
    from mlflow.entities.trace_location import UnityCatalog

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "databricks"))
    # On Databricks, distributed traces merge across services only when stored
    # in a Unity Catalog schema — target the same experiment + UC schema as the
    # Databricks agent. Requires MLFLOW_TRACING_SQL_WAREHOUSE_ID.
    _uc = None
    if os.environ.get("MLFLOW_UC_CATALOG") and os.environ.get("MLFLOW_UC_SCHEMA"):
        _uc = UnityCatalog(
            catalog_name=os.environ["MLFLOW_UC_CATALOG"],
            schema_name=os.environ["MLFLOW_UC_SCHEMA"],
            table_prefix=os.environ.get("MLFLOW_UC_TABLE_PREFIX", "dtdemo"),
        )
    if os.environ.get("MLFLOW_EXPERIMENT"):
        mlflow.set_experiment(
            experiment_name=os.environ["MLFLOW_EXPERIMENT"], trace_location=_uc
        )
    # Auto-trace OpenAI-compatible LLM calls into MLflow (analog of LangSmith's
    # wrap_openai). Graph nodes are captured by the @traced decorators below, so
    # mlflow.langchain.autolog() (which needs the full `langchain` package) isn't
    # required.
    mlflow.openai.autolog()

if _USE_LANGSMITH:
    from langsmith import traceable
    from langsmith.run_helpers import get_current_run_tree
    from langsmith.wrappers import wrap_openai


def traced(run_type: str, name: str):
    """Decorator recording a span in whichever backend(s) are active."""

    def decorator(fn):
        wrapped = fn
        if _USE_LANGSMITH:
            wrapped = traceable(run_type=run_type, name=name)(wrapped)
        if _USE_MLFLOW:
            from mlflow.entities import SpanType

            span_type = {"tool": SpanType.TOOL, "chain": SpanType.CHAIN}.get(
                run_type, SpanType.CHAIN
            )
            wrapped = mlflow.trace(name=name, span_type=span_type)(wrapped)
        return wrapped

    return decorator


def outgoing_trace_headers() -> dict[str, str]:
    """Trace-context headers to forward to the Databricks agent, per backend.

    LangSmith: langsmith-trace + baggage (from the active run tree).
    MLflow:    W3C traceparent (from the active span).
    'both':    merge — the remote agent re-opens whichever it's configured for.
    """
    headers: dict[str, str] = {}
    if _USE_LANGSMITH and (run_tree := get_current_run_tree()):
        headers.update(run_tree.to_headers())
    if _USE_MLFLOW:
        headers.update(mlflow.tracing.get_tracing_context_headers_for_http_request())
    return headers


def _fmapi_client():
    """Resolve an FMAPI client (version-independent across databricks-sdk builds).

    Works from a Databricks profile (OAuth auto-refresh), from explicit
    host/token env vars, or from ambient credentials (e.g. inside a Databricks
    notebook). LangSmith needs the client wrapped to emit LLM spans; MLflow
    captures them via the global openai.autolog() patch.
    """
    from databricks.sdk import WorkspaceClient
    from openai import OpenAI

    if os.environ.get("DATABRICKS_TOKEN") and os.environ.get("DATABRICKS_HOST"):
        host = os.environ["DATABRICKS_HOST"].rstrip("/")
        client = OpenAI(
            api_key=os.environ["DATABRICKS_TOKEN"],
            base_url=f"{host}/serving-endpoints",
        )
    else:
        profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
        cfg = (WorkspaceClient(profile=profile) if profile else WorkspaceClient()).config
        token = cfg.authenticate()["Authorization"].split(" ", 1)[1]
        client = OpenAI(api_key=token, base_url=f"{cfg.host.rstrip('/')}/serving-endpoints")

    if _USE_LANGSMITH:
        client = wrap_openai(client)
    return client


class InquiryState(TypedDict, total=False):
    inquiry: str
    product: str
    adverse_events: list[str]
    databricks_response: dict
    final_answer: str


# ─── Nodes ───────────────────────────────────────────────────────────────────
def triage(state: InquiryState) -> InquiryState:
    """Classify the inquiry: extract the product and flag possible adverse events."""
    client = _fmapi_client()
    resp = client.chat.completions.create(
        model=FMAPI_ENDPOINT,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You triage inbound medical-information inquiries for a pharma "
                    "company. Extract the single product name being asked about and "
                    "list any symptoms/events the patient reportedly experienced "
                    "(potential adverse events for pharmacovigilance). "
                    'Respond with strict JSON: {"product": str, "adverse_events": [str]}.'
                ),
            },
            {"role": "user", "content": state["inquiry"]},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    # Foundation models sometimes wrap JSON in fences; strip them defensively.
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    parsed = json.loads(raw)
    return {
        "product": parsed.get("product", ""),
        "adverse_events": parsed.get("adverse_events", []),
    }


@traced(run_type="tool", name="call_databricks_agent")
def _call_databricks_agent(payload: dict) -> dict:
    """HTTP call to the Databricks agent, propagating trace context.

    The forwarded headers (langsmith-trace/baggage and/or W3C traceparent) let
    the remote service continue this exact trace — see outgoing_trace_headers.
    """
    headers = outgoing_trace_headers()

    # When talking to a deployed Databricks App (not localhost), authenticate.
    if not DATABRICKS_AGENT_URL.startswith("http://localhost") and os.environ.get(
        "DATABRICKS_TOKEN"
    ):
        headers["Authorization"] = f"Bearer {os.environ['DATABRICKS_TOKEN']}"

    resp = requests.post(
        DATABRICKS_AGENT_URL, json=payload, headers=headers, timeout=120
    )
    resp.raise_for_status()
    return resp.json()


def call_databricks_agent(state: InquiryState) -> InquiryState:
    response = _call_databricks_agent(
        {
            "inquiry": state["inquiry"],
            "product": state.get("product", ""),
            "adverse_events": state.get("adverse_events", []),
        }
    )
    return {"databricks_response": response}


def compose(state: InquiryState) -> InquiryState:
    """Wrap the Databricks agent's draft with the required disclaimer."""
    dbx = state.get("databricks_response", {})
    draft = dbx.get("draft", "(no draft returned)")
    ae = state.get("adverse_events", [])

    ae_note = ""
    if ae:
        ae_note = (
            "\n\n**Pharmacovigilance note:** The following were flagged as possible "
            f"adverse events and have been recorded for reporting: {', '.join(ae)}."
        )

    disclaimer = (
        "\n\n---\n_This response is intended for healthcare professionals and is "
        "based on approved product information. It is not a substitute for "
        "individual clinical judgment._"
    )

    return {"final_answer": draft + ae_note + disclaimer}


# ─── Graph ─────────────────────────────────────────────────────────────────
def build_graph():
    g = StateGraph(InquiryState)
    g.add_node("triage", triage)
    g.add_node("call_databricks_agent", call_databricks_agent)
    g.add_node("compose", compose)
    g.add_edge(START, "triage")
    g.add_edge("triage", "call_databricks_agent")
    g.add_edge("call_databricks_agent", "compose")
    g.add_edge("compose", END)
    return g.compile()


graph = build_graph()


@traced(run_type="chain", name="field_medical_intake")
def run_inquiry(inquiry: str) -> dict:
    """Top-level entry point — this is the root of the unified trace."""
    return graph.invoke({"inquiry": inquiry})
