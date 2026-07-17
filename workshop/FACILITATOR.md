# Facilitator guide — running the distributed-tracing workshop

This is the **one-time workspace prep** for running
`distributed_tracing_workshop.py` with a group. Participants each run the notebook
on their own; everything they create is auto-namespaced to their username, so the
only shared resources are a SQL warehouse and read-only reference data.

Audience assumption: you have workspace **admin** or a power-user with Unity Catalog
+ warehouse + experiment permissions. Participants need only ordinary user access
plus the grants below.

---

## What each participant creates (automatically — no coordination needed)

Derived from their `current_user()` in Module 0, e.g. `jane.doe@example.com`:

| Resource | Value | Isolation |
|----------|-------|-----------|
| MLflow experiment | `/Users/<user>/dtrace-workshop` | user folder — private by default |
| UC trace schema | `<WORKSHOP_CATALOG>.dtrace_<user_slug>` | one schema per person |
| MLflow trace tables | `…dtrace_<slug>.traces_otel_spans` etc. | in their own schema |
| LangSmith project | `dtrace-<user_slug>` | one project per person |
| Local agent | FastAPI on `127.0.0.1:8010` **on their own driver** | no cross-participant port collisions |

Shared: the SQL warehouse and the read-only `main.field_medical` reference data.

---

## Prep checklist (do once, before the session)

### 1. Pick / create a catalog participants can create schemas in

```sql
CREATE CATALOG IF NOT EXISTS workshop;
-- Let all participants create their own dtrace_<user> schema in it:
GRANT USE CATALOG   ON CATALOG workshop TO `<participants-group>`;
GRANT CREATE SCHEMA ON CATALOG workshop TO `<participants-group>`;
```

`<participants-group>` is whatever account group your attendees are in (or grant to
`account users` for an internal event). Each participant's schema is created by their
own notebook run in Module 0.3.

### 2. Provide a shared SQL warehouse

Any running warehouse works (serverless is easiest). Grant the group `CAN USE`:

```
Warehouse → Permissions → add <participants-group> → Can use
```

Note its **warehouse ID** (Warehouse → Connection details, or the URL) — participants
paste it into Module 0.1.

### 3. Seed the reference data (once, shared read-only)

Run [`../shared/databricks_agent/seed_data.sql`](../shared/databricks_agent/seed_data.sql)
on the warehouse. It creates `main.field_medical.products` + `.adverse_events`
(3 fictional drugs). Grant read to the group:

```sql
GRANT USE CATALOG ON CATALOG main                       TO `<participants-group>`;
GRANT USE SCHEMA  ON SCHEMA  main.field_medical         TO `<participants-group>`;
GRANT SELECT      ON SCHEMA  main.field_medical         TO `<participants-group>`;
```

(If `main` is locked down, seed into any shared catalog and set `DATA_SCHEMA`
accordingly in Module 0.1.)

### 4. Foundation Model access

Confirm a chat FM endpoint exists and the group can query it (default
`databricks-claude-sonnet-4-5`). Participants set `FMAPI_ENDPOINT` in Module 0.1.

### 5. (Optional) LangSmith keys for Module B

The MLflow scenario needs no external key. To also show the LangSmith backend
(`TRACING_BACKEND=both`), give each participant a LangSmith API key. Cleanest is a
secret scope so keys aren't pasted in plaintext:

```bash
databricks secrets create-scope workshop
databricks secrets put-secret workshop langsmith-api-key --string-value "lsv2_pt_..."
databricks secrets put-acl   workshop <participants-group> READ
```

Then participants set `LANGSMITH_API_KEY = dbutils.secrets.get("workshop", "langsmith-api-key")`
in Module 0.1. (A shared key is fine — traces still separate by per-user project.)
Leaving the key blank runs MLflow-only.

### 6. Distribute the code

**Git folder (recommended).** Point participants at the public repo:
`https://github.com/satxsam/distributed-tracing-examples`. Each person adds it via
**Workspace → their home folder → Create → Git folder** (older UIs: **Repos → Add
Repo**), pastes the URL, and opens `workshop/distributed_tracing_workshop` from
**inside** the Git folder — the notebook auto-detects `shared/` relative to itself.
No GitHub token is needed for a public repo.

**Zip alternative.** If Git folders aren't available, hand out a zip of the repo;
participants **Workspace → Import** the whole folder (preserving `shared/`, `workshop/`).
Either way, if the notebook is moved somewhere the sibling `shared/` no longer resolves,
set `REPO_ROOT` manually near the top of Module 0.2.

### 7. Compute

Any cluster/serverless with internet egress and Python 3.10+. The notebook
`%pip install`s its own deps and launches the agent as a subprocess on the driver —
no cluster libraries to pre-install. One driver per participant (don't share a single
cluster's driver across many people, since each launches a local agent on port 8010).

---

## Hand-out to participants

Give them: the **catalog name**, the **warehouse ID**, the **data schema**, the FM
endpoint, and (if using LangSmith) how to get the key. They fill in Module 0.1 —
five values — and run top to bottom. Modules are self-documenting.

---

## During the session

- Module A (MLflow) is the core payoff; everyone should reach a green "✅ Unified" check.
- Module B just opens LangSmith to show the *same* run in a second backend (only if
  keys were provided).
- Module C is a read-only discussion of the reverse direction — no one deploys anything.
- The notebook's final cell stops each participant's local agent.

## Teardown (after the session)

Trace data persists in each participant's experiment + UC schema. To reclaim:

```sql
-- drop all participant trace schemas at once (they share the dtrace_ prefix)
SHOW SCHEMAS IN workshop LIKE 'dtrace_*';
-- then: DROP SCHEMA workshop.dtrace_<slug> CASCADE;  (per participant)
```

Experiments live under each user's `/Users/<user>/dtrace-workshop` and can be deleted
from the Experiments UI. No Apps are created by this workshop, so there's no App
compute to stop.

---

## Why local-agent instead of a shared deployed App?

The server side records its MLflow spans into whatever experiment **it** is configured
with (the propagated `traceparent` header carries only the trace *id*, not a
destination). A single shared App would therefore collect every participant's
server-side spans into one experiment. Running the agent locally on each participant's
driver keeps each person's full trace — both sides — in their own experiment, while
still exercising a real HTTP/trace-context boundary. See
[`../scenario_2_mlflow/README.md`](../scenario_2_mlflow/README.md) for the deployed-App
production pattern.
