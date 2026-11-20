# Agentic SDLC Prototype

A dependency-free Python prototype that turns one of three sample requirements into a reviewable, run-scoped engineering outcome. Its greenfield scenario is a working URL shortener with analytics; the orchestration demonstrates a governed workflow engine, not an autonomous production coding agent. Scenario agents are deterministic fixtures and no LLM or external service is called.

## Quick Start

Requires Python 3.10 or newer.

```powershell
python -m unittest discover -s tests -v
python -m agentic_sdlc run --scenario greenfield --approve all
```

Run the standalone URL shortener service:

```powershell
python -m url_shortener
```

The server binds to `127.0.0.1:8000` by default and stores data in `url-shortener.sqlite3`. Create a link with `POST /api/urls` and JSON `{"url":"https://example.com"}`; visit the returned `short_url` to redirect; query `GET /api/urls/{code}/stats` for click count and timestamps. `GET /health` provides a basic health response. Host, port, and database path can be set with `URL_SHORTENER_HOST`, `URL_SHORTENER_PORT`, and `URL_SHORTENER_DB` or command-line arguments.

Without `--approve all`, the workflow pauses at a human checkpoint. Approve and resume explicitly:

```powershell
python -m agentic_sdlc run --scenario brownfield --workspace .
python -m agentic_sdlc resume <run-id> --approve design
python -m agentic_sdlc resume <run-id> --approve release
```

Artifacts, `state.json`, and append-only `audit.jsonl` are written under `.agentic-runs/<run-id>/`. Brownfield inventory is limited to 500 files and excludes common dependency/build directories. Generated content never edits the selected workspace.

Install as a CLI if desired:

```powershell
python -m pip install -e .
agentic-sdlc run --scenario ambiguous --approve all
```

## Architecture

`WorkflowEngine` validates the explicit dependency DAG, executes each ready frontier concurrently, then synchronizes dependent tasks on the next frontier. The sample graph is:

```text
understand -> decompose -> design -> [human design approval] -> implement
                                                               |          |
                                                               v          v
                                                      unit validation   docs
                                                               \          /
                                                        integration validation
                                                                  |
                                                        [human release approval]
                                                                  |
                                                          release readiness
```

Each task gets a snapshot of upstream context; outputs and decisions are persisted in the run state. Approval tasks pause safely and can be resumed from disk. `WorkflowEngine.replan()` accepts a revised completed upstream output, invalidates only dependent tasks, and removes their stale artifacts. Retries are bounded per task, an optional fallback handler is supported, and terminal failures or policy violations trigger a safe stop and rollback of generated run artifacts.

Audit events record timestamps, task transitions, approvals, retries/fallback outcomes, policy failures, and rollback. The engineering summary reports per-run completed-task success rate, retry count/frequency, rollback count/frequency, mean recovery time when a retry/fallback succeeds, and elapsed latency at report generation. These are run-level signals, not fleet-level trend aggregation or latency percentiles.

## Scenarios

- `greenfield`: packages the URL shortener implementation, API schema, and tests as run-scoped artifacts, then statically validates and documents them.
- `brownfield`: inventories the selected workspace and proposes where to add URL-shortener analytics, plus a compatibility contract. The inventory is evidence, not semantic code understanding, and no source is modified.
- `ambiguous`: makes missing URL-shortener product and operational decisions explicit, preserves them as risks, and withholds implementation pending clarification.

All scenarios decompose work, branch implementation validation and documentation in parallel, synchronize before release review, and require the same approval checkpoints.

## URL Shortener API and Assumptions

- `POST /api/urls` accepts an HTTP(S) destination and returns a generated short code and URL.
- `GET /{code}` redirects to the destination and atomically increments its click count.
- `GET /api/urls/{code}/stats` returns the destination, creation time, click count, and last access time.
- `GET /health` returns a basic service status.

SQLite provides durable local storage, uniqueness constraints, and transactional click updates. This prototype intentionally leaves authentication, link expiration, analytics retention/privacy, distributed deployment, and service-level targets undefined; these need product and operational requirements before production deployment. The stats endpoint is public, so deploy only in a trusted environment until access control is designed.

## Safety and Validation

- Artifact paths are constrained to the current run directory; existing artifacts are not overwritten.
- Generated artifacts are scanned for likely inline credentials. A violation stops the workflow and rolls back generated files.
- High-impact implementation and release readiness are gated by explicit approvals.
- Generated Python is parsed with `ast`; JSON artifacts are parsed. Generated code is not imported or executed.
- Unit tests exercise pause/resume, parallel synchronization, bounded retry/fallback, policy safe-stop, downstream-only replanning, and URL-shortener API/persistence/analytics behavior.

## Trade-offs and Limitations

The prototype uses deterministic handlers so it is reproducible and needs no credentials. Replace the scenario handlers with constrained model/tool adapters to connect real agents; preserve the handler contract, policy checks, approval gates, and artifact boundary. Brownfield reasoning is a bounded filename inventory with simple heuristics, not call-graph or semantic analysis. There is no distributed queue, multi-user authorization, tamper-evident audit signing, external test sandbox, or deployment integration. The local JSON state store is suitable for a prototype, not concurrent multi-process production use.