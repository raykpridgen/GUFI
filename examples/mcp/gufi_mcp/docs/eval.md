# Agent evaluation (GUFI MCP)

Implementation and design live at the **project root**:

| Artifact | Purpose |
|----------|---------|
| [eval_plan.md](../eval_plan.md) | Requirements, metrics, example `gufi_query` ground truths |
| [eval_design.md](../eval_design.md) | Architecture, resolved decisions, logging layout |
| [gen_index.py](../gen_index.py) | Synthetic filesystem + index generation |
| [gufi_eval.py](../gufi_eval.py) | Trial orchestration (`run`, `check`) |
| [gufi_eval_server.py](../gufi_eval_server.py) | GUFI MCP server with a tool allowlist (`GUFI_EVAL_TOOLS`) |
| [eval_lib/](../eval_lib/) | Registry, MCP session and telemetry, drivers, comparator, metrics, reporter |
| `scenarios.single.yaml` | Single-query scenarios |
| `scenarios.general.yaml` | General-usage scenarios |

Default OpenRouter model for **agent** runs: **`openai/gpt-4o-mini`** (`gufi_eval.py --model`).

The `eval/` directory is deprecated; use it only for extra natural-language prompt ideas.

## Generating the master index

```text
uv run python gen_index.py --config gen_index.example.yaml [--seed N] [--fixed-now 2026-10-01]
                           [--index-name NAME] [--export-root DIR] [--ownership host|preserve]
                           [--force] [--dry-run] [--keep-plan plan.json]
```

- All random choices are made on the host in a plan; the same seed, config, and `fixed_now` reproduce the same tree. `--dry-run` prints plan statistics without Docker.
- A `--rm` Docker container (default `python:3.13-slim`, no network) runs `gen_index_apply.py` as root: it builds the tree in the container's own filesystem, runs `gufi_dir2index` and `gufi_treesummary_all` from the GUFI install bind-mounted read-only, and writes the index into the export root (default `GUFI_INDEX_ROOT`).
- Layout: `NAME/groups/<group>/` (`root:<gid> 2770`) with a `shared/` dir and one `<user>/` home (`<uid>:<gid> 0750`) per member, each holding a seeded mini-tree of directories, files, and symlinks (some dangling). Groups get gids from 2001, users uids from 3001.
- Files are really allocated (`posix_fallocate`, not sparse), so `gufi_du` reports real usage. Regular files are log-uniform in `file_size_min..file_size_max` (default up to 4 MiB) plus `large_files` drawn from `large_file_size_min..max` (default 6 files of 512 MiB to 2 GiB). `disk_limit` (default `10GiB`) is a hard cap: if the sampled sizes exceed it they are all scaled down to fit (the factor is recorded as `stats.size_scale`), the container checks free space before writing and aborts if the running total would pass the cap, and the build fails if the tree occupied more than the cap or less than its file data (sparse). The tree lives only in the container's filesystem while it is indexed.
- mtime and atime are spread back from `fixed_now`; ctime is the build time.
- The build is verified against the plan (counts and sizes per type, uid, gid) before `NAME.manifest.json` is written next to the index. The manifest holds the seed, `fixed_now`, uid/gid maps, statistics, and a `manifest_id` (plan hash).
- `index_ownership: host` (default) chowns the index to you. `preserve` keeps the synthetic owners and modes so GUFI permission enforcement applies; the build then checks that the first user sees only their group's files. A preserved index is not readable by a non-root host user, and replacing it needs `--force` (the container removes it).

## Scenario entry (both YAML files)

Every scenario in **`scenarios.single.yaml`** and **`scenarios.general.yaml`** uses the **same keys**:

```yaml
- id: ext_histogram
  prompt: "…"
  tags: []
  enabled: true
  index: eval_master
  parameters: {}
  ground_truth: { ... }   # GufiQuery-shaped object; null in general file
  notes: ""
```

| Field | Notes |
|-------|--------|
| `id` | Stable scenario id |
| `prompt` | User message to the agent |
| `tags` | List (may be empty) |
| `enabled` | Skip when false |
| `index` | Index name under `GUFI_INDEX_ROOT` |
| `parameters` | Case params (days, limits, …); `{}` if none |
| `ground_truth` | `GufiAggregateQuery` fields **without `index`** (the scenario's `index` is used) for single-query; **`null`** for general |
| `notes` | Maintainer notes; `""` if none |

Every single-query scenario is mirrored in `scenarios.general.yaml` with the same id, prompt and parameters and the tag `mirrored`, so the same questions are also asked with the full tool catalog. `test_gufi_eval.py` fails if they drift apart, or if any ground-truth statement appears in what the server shows agents (the primer, tool listings and resource listings).

Prompts are worded the way a user would ask. They name no GUFI function (`bytecat`, `agecat`, `ext`, `rpath`), column, or type code, and give no formulas or timestamps; the system prompt provides the current time. The trade-off is that some single-query prompts allow answers the exact row compare rejects: a size or age breakdown in the agent's own buckets, or dates instead of epoch seconds. Scenario `notes` record where that applies. Each general prompt asks a distinct question, and its `notes` name the nearest neighbour and how it differs. Tests fail if a prompt uses one of the giveaway terms, omits `{index}`, or if two general prompts are identical.

Prompts and ground-truth strings may use placeholders filled from the index manifest (`GUFI_INDEX_ROOT/<index>.manifest.json`) and `parameters`: `{index}`, `{fixed_now}` (epoch), `{fixed_now_iso}`, `{fixed_now_date}`, and `{parameters.NAME}`. Ground truth is validated with the server's `GufiAggregateQuery` model when the registry loads.

## Running evaluations

```text
uv run python gufi_eval.py check [--scenario ID ...]
uv run python gufi_eval.py run [--mode single|general|both] [--trials N] [--agent openrouter|replay]
                               [--model openai/gpt-4o-mini] [--temperature T] [--scenario ID ...]
                               [--concurrency 4] [--max-tool-calls 15] [--max-attempts 3] [--timeout 180]
                               [--output eval_runs]
                               [--scenarios-single PATH] [--scenarios-general PATH] [--index-manifest PATH]
```

- `check` runs every enabled single-query ground truth through the full server and flags empty or all-zero results, with no agent involved. Run it after rebuilding the index.
- `run --agent replay` is a stub agent that submits the ground truth as its only call. It should pass 100% and costs nothing, so use it to test the pipeline.
- `run` (the default `--agent openrouter`) needs `OPENROUTER_API_KEY` in `.env`. The key is never written to run artifacts.
- `tests`: `uv run pytest test_gufi_eval.py`. The replay end-to-end test needs `eval_master`. It is skipped when the whole suite runs, because `test_aggregate_validation.py` points `GUFI_INDEX_ROOT` at a fake root.

### How a trial runs

- One run starts two long-lived stdio servers through `gufi_eval_server.py`:
  - the full server (`GUFI_EVAL_TOOLS=all`), used for ground truth and general mode;
  - the slim server (`GUFI_EVAL_TOOLS=aggregate_sql_query`), used by the single-query agent. Disallowed tools are removed from it, so they are absent from `tools/list` and calling one fails with "Unknown tool".
- Every trial starts a fresh model conversation. Up to `--concurrency` trials run at once.
- Single-query trials run the ground truth first (through MCP, the same path as the agent), then the agent.
- The OpenRouter driver (`eval_lib/driver.py`):
  - **Tools:** the server's MCP tool listing becomes OpenAI function tools, with `$ref`s inlined. MCP resources are exposed through one extra `read_resource(uri)` function, so every read is visible and counted.
  - **System prompt:** includes the server's MCP instructions (the GUFI primer, `PRIMER` in `gufi_mcp_server.py`), as an MCP host would. It also names the index and gives the manifest's `fixed_now` as the current time, with a note to do date arithmetic in SQL. In single-query mode it says the agent may call `aggregate_sql_query` up to `--max-attempts` times and should fix the query and retry when a result is wrong. Scenario notes, tags and ground truth are never sent.
  - **Limits:** a trial ends at the first reply without tool calls, at `--max-tool-calls` (resource reads included), at `--timeout`, or in single-query mode when the agent asks for an aggregate call beyond `--max-attempts` (`stop_reason` `max_attempts`). Tool results sent to the model are cut at 20,000 characters, and the trace notes it.
- **Grading (single-query):** every `aggregate_sql_query` call up to `--max-attempts` is an attempt, failed calls included. The last successful attempt is the agent's answer and is the only one graded: it passes if it ran on the scenario's index and returns the ground truth rows. A failed call does not replace an earlier answer, but a later successful one does, even if an earlier attempt matched (tagged `replaced_match`). `--max-attempts 1` is strict one-shot grading.
- **Comparison** (`eval_lib/comparator.py`):
  - Values are normalized: numeric strings become numbers, floats are rounded to 6 decimal places, and whole floats become ints.
  - Rows are sorted unless the ground truth's top-level `final_sql` has `ORDER BY`.
  - Column names are ignored, and a permutation of the same columns is accepted (it is recorded as `column_permutation` in the similarity log).
  - A truncated agent result fails.
- **Timing:** `aggregate_sql_query` calls are serialized inside the server by the gufi_vt lock, so with `--concurrency` above 1, `timing_delta_ms` can include time spent waiting for another trial's query. Use `--concurrency 1` when timing matters.

### Metrics

| Metric | Definition |
|--------|------------|
| `passed_on_attempt` | 1-based attempt of the graded call when the trial passed, or `null` (single). |
| `one_shot_pass` | `true` when the trial passed with a single `aggregate_sql_query` call (single). |
| `resources_consumed` | Resource reads before the first `aggregate_sql_query` call; repeated reads count. |
| `aggregate_revision_count` | Number of `aggregate_sql_query` calls minus one. |
| `timing_delta_ms` | Agent's graded call duration minus the ground truth's duration; both include MCP overhead. |
| `failure_tags` | Any of `wrong_result`, `wrong_index` (the graded call ran on another index), `tool_error`, `timeout`, `no_gradable_output`, `max_tool_calls`, `max_attempts`, `replaced_match` (an earlier attempt matched but a later successful one did not), `multiple_aggregate` (informational). |
| `executions` | `[resource_reads, tool_calls]` (both modes). |
| `execution_time_ms` | Trial wall time including tool latency (both modes). |
| `redundancies` | `[name, count - 1]` for each tool or resource URI used more than once (both modes). |

`manifest.json` results report the single-query pass rate within `--max-attempts`, the one-shot pass rate, the cumulative pass rate by attempt (`single_pass_rate_by_attempt`, e.g. `{1: 0.4, 2: 0.6, 3: 0.7}`), mean tool calls, and pass and one-shot rates per scenario. Each similarity log has `attempt_results` with the outcome of every graded attempt.

## Run artifacts

```text
eval_runs/<run_id>/
  config.json        mode, trials, agent and model, limits, scenario files, index seed and fixed_now
  manifest.json      git rev and dirty flag, gufi version, package versions, registry sha256,
                     index manifest ids, agent config and hash, results (mean pass rate per scenario, cost)
  summary.jsonl      one line per trial
  single/<id>/trial_<n>.similarity.json   ground truth vs agent query, attempts, row counts, reason
  single/<id>/trial_<n>.rows.json         expected vs actual rows (first 1000), durations
  single/<id>/trial_<n>.trace.json        events, aggregate calls, transcript, usage
  general/<id>/trial_<n>.response.txt     final assistant message
  general/<id>/trial_<n>.trace.json
```

Trace events are `{t_ms, kind, name, args_summary, ok, duration_ms, error, note}`.

## Trial line (`summary.jsonl`)

One JSON object per trial with the **same keys** in every mode. Unused metrics are `null`, `[]`, or `""`, never omitted:

`scenario_id, trial, mode, pass, passed_on_attempt, one_shot_pass, resources_consumed, timing_delta_ms, similarity_path, executions, execution_time_ms, redundancies, final_response_path, aggregate_revision_count, failure_tags, agent, model, stop_reason, duration_ms, usage, trace_path, error`

`pass` is `null` in general mode. `usage` holds prompt, completion, and total tokens, the request count, and the OpenRouter cost in USD.
