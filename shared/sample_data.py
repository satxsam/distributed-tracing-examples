"""Fictional field-medical reference data + a self-service seeder.

This is the single source of truth for the workshop's sample data. The drugs,
doses, and adverse events are entirely made up.

`seed(...)` creates two tables — `<catalog>.<schema>.products` and
`.adverse_events` — and populates them. It runs the DDL/DML through a Databricks
SQL warehouse via the SQL Statement Execution API, so the same function works
from a notebook, from the local agent process, or from a laptop. Each participant
seeds their own schema; no shared/admin setup is required beyond being able to
CREATE SCHEMA in the target catalog.
"""
from __future__ import annotations

# ── Sample data (fictional) ──────────────────────────────────────────────────
PRODUCTS = [
    ("Cardizafen", "cardizafen hydrochloride", "calcium-channel blocker (antihypertensive)",
     "180 mg once daily, titratable to 360 mg/day",
     "In severe renal impairment (CrCl < 30 mL/min), initiate at 90 mg once daily and "
     "titrate slowly with close BP and renal monitoring. No dose adjustment required in "
     "mild-to-moderate impairment.",
     "Reduce starting dose in moderate-to-severe hepatic impairment; monitor LFTs.",
     "Category C: use only if potential benefit justifies potential risk to the fetus. "
     "Limited human data."),
    ("Neuroliximab", "neuroliximab", "monoclonal antibody (anti-CGRP, migraine prophylaxis)",
     "240 mg subcutaneous loading dose, then 120 mg monthly",
     "No dose adjustment required in renal impairment; not studied in severe (CrCl < 15).",
     "No dose adjustment required in hepatic impairment.",
     "Insufficient data; discontinue if pregnancy is confirmed unless clearly needed."),
    ("Glucoravir", "glucoravir sodium", "SGLT2 inhibitor (type 2 diabetes)",
     "10 mg once daily, may increase to 25 mg once daily",
     "Do not initiate if eGFR < 30 mL/min/1.73m2; discontinue if eGFR falls persistently "
     "below 45.",
     "Not recommended in severe hepatic impairment.",
     "Not recommended during the second and third trimesters."),
]

ADVERSE_EVENTS = [
    ("Cardizafen", "peripheral edema", "common", "non-serious"),
    ("Cardizafen", "headache", "common", "non-serious"),
    ("Cardizafen", "bradycardia", "uncommon", "serious"),
    ("Cardizafen", "palpitations", "common", "non-serious"),
    ("Cardizafen", "dizziness", "common", "non-serious"),
    ("Neuroliximab", "injection-site reaction", "common", "non-serious"),
    ("Neuroliximab", "constipation", "common", "non-serious"),
    ("Neuroliximab", "hypersensitivity reaction", "rare", "serious"),
    ("Glucoravir", "genital mycotic infection", "common", "non-serious"),
    ("Glucoravir", "volume depletion", "uncommon", "non-serious"),
    ("Glucoravir", "diabetic ketoacidosis", "rare", "serious"),
]

PRODUCT_COLUMNS = (
    "product_name STRING, generic_name STRING, drug_class STRING, standard_dose STRING, "
    "renal_guidance STRING, hepatic_guidance STRING, pregnancy_guidance STRING"
)
ADVERSE_EVENT_COLUMNS = (
    "product_name STRING, adverse_event STRING, frequency STRING, seriousness STRING"
)


def _esc(v: str) -> str:
    return v.replace("'", "''")


def _values_clause(rows: list[tuple]) -> str:
    return ",\n".join("('" + "','".join(_esc(c) for c in row) + "')" for row in rows)


def seed_statements(catalog: str, schema: str) -> list[str]:
    """The SQL statements that create + populate the two tables (idempotent)."""
    tbl = f"{catalog}.{schema}"
    return [
        f"CREATE SCHEMA IF NOT EXISTS {tbl} COMMENT 'Field-medical demo reference data'",
        f"CREATE TABLE IF NOT EXISTS {tbl}.products ({PRODUCT_COLUMNS}) USING DELTA",
        f"CREATE TABLE IF NOT EXISTS {tbl}.adverse_events ({ADVERSE_EVENT_COLUMNS}) USING DELTA",
        f"TRUNCATE TABLE {tbl}.products",
        f"INSERT INTO {tbl}.products VALUES\n{_values_clause(PRODUCTS)}",
        f"TRUNCATE TABLE {tbl}.adverse_events",
        f"INSERT INTO {tbl}.adverse_events VALUES\n{_values_clause(ADVERSE_EVENTS)}",
    ]


def seed(catalog: str, schema: str, *, warehouse_id: str, workspace_client=None) -> str:
    """Create + populate the reference tables via a SQL warehouse.

    Args:
        catalog, schema: where to create `products` and `adverse_events`.
        warehouse_id: a SQL warehouse the caller can use.
        workspace_client: an existing databricks.sdk.WorkspaceClient; if omitted,
            a default one is created (uses ambient / profile credentials).

    Returns the fully-qualified schema name.
    """
    if workspace_client is None:
        from databricks.sdk import WorkspaceClient

        workspace_client = WorkspaceClient()

    for stmt in seed_statements(catalog, schema):
        r = workspace_client.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=stmt, wait_timeout="50s"
        )
        state = getattr(r.status, "state", None)
        if str(state) not in ("StatementState.SUCCEEDED", "SUCCEEDED"):
            msg = r.status.error.message if r.status.error else state
            raise RuntimeError(f"seed failed on: {stmt[:60]}... -> {msg}")
    return f"{catalog}.{schema}"


def render_sql(catalog: str = "main", schema: str = "field_medical") -> str:
    """Render the seed as a standalone .sql script (used to regenerate seed_data.sql)."""
    header = (
        "-- Field-medical reference data for the Databricks agent (FICTIONAL).\n"
        "-- Generated from shared/sample_data.py — edit that file, not this one.\n"
        f"-- Default target: {catalog}.{schema}. Run on a SQL warehouse.\n\n"
    )
    return header + ";\n\n".join(seed_statements(catalog, schema)) + ";\n"


if __name__ == "__main__":
    # `python -m sample_data` prints the .sql (handy for regenerating seed_data.sql).
    print(render_sql())
