"""CLI: fire an inquiry through the LangGraph -> Databricks flow and print where
to view the unified trace (LangSmith URL and/or MLflow experiment).

    python -m langgraph_agent.run "How should Cardizafen be dosed in severe renal impairment? Patient reported palpitations."

The observability backend is chosen by TRACING_BACKEND (langsmith | mlflow | both).
"""
from __future__ import annotations

import contextlib
import os
import sys

from dotenv import find_dotenv, load_dotenv

# Load the repo-root .env regardless of the current working directory (so this
# works whether launched from the repo root or from shared/).
load_dotenv(find_dotenv(usecwd=True))

DEFAULT_INQUIRY = (
    "What is the recommended dosing of Cardizafen in patients with severe renal "
    "impairment? A patient also reported palpitations after starting it."
)


def _print_langsmith_trace(project: str) -> None:
    try:
        from langsmith import Client

        runs = list(Client().list_runs(project_name=project, is_root=True, limit=1))
        if runs:
            print(f"\nLangSmith trace: {runs[0].url}")
    except Exception as e:  # noqa: BLE001 — link is best-effort
        print(f"\n(Open LangSmith project '{project}' to view the trace: {e})")


def _print_mlflow_trace() -> None:
    exp = os.environ.get("MLFLOW_EXPERIMENT", "(default experiment)")
    print(f"\nMLflow experiment: {exp}")
    print("  → open it in the Databricks workspace (Experiments → Traces).")


def main() -> None:
    inquiry = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INQUIRY

    # Import after load_dotenv so LANGSMITH_* / DATABRICKS_* / MLFLOW_* are set.
    from langgraph_agent.agent import (
        _USE_LANGSMITH,
        _USE_MLFLOW,
        TRACING_BACKEND,
        run_inquiry,
    )

    project = os.environ.get("LANGSMITH_PROJECT", "default")
    print(f"→ Inquiry: {inquiry}\n→ Tracing backend: {TRACING_BACKEND}\n")

    # Scope LangSmith runs to the configured project when LangSmith is active.
    ctx = contextlib.nullcontext()
    if _USE_LANGSMITH:
        from langsmith.run_helpers import tracing_context

        ctx = tracing_context(project_name=project)

    with ctx:
        result = run_inquiry(inquiry)

    print("─── Final answer ─────────────────────────────────────────────")
    print(result.get("final_answer", "(no answer)"))
    print("──────────────────────────────────────────────────────────────")

    if _USE_LANGSMITH:
        _print_langsmith_trace(project)
    if _USE_MLFLOW:
        _print_mlflow_trace()


if __name__ == "__main__":
    main()
