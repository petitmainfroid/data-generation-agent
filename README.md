# data-generation-agent

Fail-closed, resumable tooling for Feishu-backed question review, generation,
augmentation, and result synchronization.

The full product requirements, architecture, module plan, permissions, memory
design, and Harness activation standards are in `docs/PRD.md`.

## Current status

This repository contains two extracted Lao adapters and a tested local durable
Harness for the larger Agent:

- `lao-quality-review`: two-stage question classification and quality review.
- `lao-difficulty-prescreen`: one GPT-5.5 answer and one Qwen answer used only
  as a coarse difficulty screen.
- `data-agent`: SQLite-backed lifecycle, immutable artifacts, events, leases,
  budgets, trials, Outbox storage, recovery, and reconciliation commands.

The canonical pipeline is:

```text
quality_review
-> difficulty_prescreen
-> consistency_review
-> answer_synthesis
-> qwen_passrate_review
-> final_gate
```

The final four stages are disabled placeholders, so the full pipeline is not
implemented and cannot emit `FINAL_ACCEPTED`. The quality and difficulty
prescreen stages are independently enabled, but `full_pipeline_enabled` remains
false until every required stage passes its own release gate.

The prescreen is not passrate. It preserves:

```text
HARD  -> PASS to consistency review
EASY  -> REJECT_TOO_EASY
ERROR -> QUARANTINE
passrate = null
```

## Local verification

The legacy source is discovered from a sibling `laokuoyang` directory or the
`LAOKUOYANG_ROOT` environment variable. Do not copy its data, prompts, or
credentials into this public repository.

Run the offline suite:

```powershell
python -m pytest -p no:cacheprovider
```

Inspect the complete pipeline without creating a database or making network
calls:

```powershell
data-agent preflight --project-root .
data-agent status --project-root .
```

Lifecycle commands are:

```text
data-agent run | status | resume | retry | cancel | reconcile
```

Read a registered Feishu seed table into a durable snapshot:

```powershell
data-agent ingest --project-root . `
  --profile configs/feishu/dev_base_profile.local.json `
  --db runs/dev/harness.sqlite3 `
  --artifact-root runs/dev/artifacts
```

See `docs/FEISHU_INGESTION.md` for the local profile and resume contract.

`run` fails with `BLOCKED_NOT_IMPLEMENTED` before creating a Job while the
complete pipeline is disabled. `retry` accepts only failures durably marked as
retryable; business rejects cannot be retried through this command.

Feishu side effects are dispatched separately:

```powershell
data-agent rebuild-counters --project-root . --db runs/dev/harness.sqlite3 `
  --job-id <job> --machine-target 100 --qualified-target 50
data-agent sync-feishu --project-root . `
  --profile configs/feishu/dev_base_profile.local.json `
  --db runs/dev/harness.sqlite3 --worker-id feishu-worker-1
```

See `docs/FEISHU_SYNC.md` for business-key idempotency, lost-ACK recovery, and
counter formulas.

Preflight the extracted tools:

```powershell
lao-quality-review --preflight-only --legacy-root ..\laokuoyang
lao-difficulty-prescreen --preflight-only --legacy-root ..\laokuoyang
```

Run quality review:

```powershell
lao-quality-review `
  --input questions.jsonl `
  --output-dir runs\quality `
  --legacy-root ..\laokuoyang `
  --env-file C:\path\to\local\.env
```

Run the verified prescreen:

```powershell
lao-difficulty-prescreen `
  --input quality_accepted.jsonl `
  --output-dir runs\difficulty-prescreen `
  --legacy-root ..\laokuoyang `
  --env-file C:\path\to\local\.env
```

The currently verified judge gateway is the temporary default Qwen route. The
legacy direct `QWEN_*` route is an explicit opt-out via `--qwen-direct`.

Each adapter writes normalized `results.jsonl` and `summary.json` alongside its
legacy artifacts. Model-facing tools do not write Feishu directly; the planned
production design uses a field-allowlisted, idempotent Outbox with readback
reconciliation.
