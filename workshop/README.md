# Workshop — distributed tracing, run entirely on Databricks

`distributed_tracing_workshop.py` walks a group through the two scenarios **with no
per-participant setup beyond five config values**. Everything runs inside Databricks:
the orchestrator runs in the notebook, and it calls a Databricks agent that the
notebook launches as a local background process on the cluster driver.

> **Why run the orchestrator in a notebook?** Convenience only. The LangGraph
> orchestrator is plain Python and runs anywhere. In a workshop, a notebook guarantees
> a working runtime and ambient auth so nobody fights their local environment. The
> notebook says this repeatedly on purpose.

> **Why a local agent (not a shared deployed App)?** So each participant's traces stay
> in *their own* experiment. A shared App would funnel everyone's server-side MLflow
> spans into one experiment. The local agent is a real HTTP/trace boundary, needs no
> deployment, and keeps each person isolated. Production would use a deployed App —
> see [`../scenario_2_mlflow/README.md`](../scenario_2_mlflow/README.md).

## For participants

**Get the code into your workspace** (whichever your facilitator chose):
- **Zip upload**: unzip the `distributed-tracing-examples` folder, then in the workspace
  UI use **Workspace → (your folder) → Import → File/Folder** and upload the whole
  `distributed-tracing-examples` folder so the structure (`shared/`, `workshop/`) is
  preserved. Open `workshop/distributed_tracing_workshop`.
- **Git folder**: **Repos → Add Repo**, paste the repo URL, open
  `workshop/distributed_tracing_workshop` from inside it.

**Run it**: attach the notebook to **Serverless** (or any cluster with internet egress),
fill in the widget fields at the top (catalog, warehouse id, data schema, FM endpoint,
optional LangSmith key), and run top to bottom. The notebook `%pip install`s its own
deps and launches the agent for you. Every other resource is auto-namespaced to your username — many people can
run in one workspace without colliding. The modules are self-documenting:

- **0 — Setup**: install deps, derive your namespace, create your UC trace schema,
  launch your local agent.
- **A — MLflow**: run the flow → one unified trace in your MLflow experiment.
- **B — Both backends** (optional): if you set a LangSmith key, the same run also
  appears as a unified trace in LangSmith.
- **C — Advanced** (read-only): the genuinely-reversed direction (deploy LangGraph as
  its own App), explained but not run.

## For facilitators

See **[FACILITATOR.md](FACILITATOR.md)** for the one-time workspace prep: the shared
warehouse, a catalog participants can create schemas in, seeding the reference data,
optional LangSmith keys, and importing the repo as a Git folder.
