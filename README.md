# data-generation-agent

Fail-closed, resumable tooling for Feishu-backed question review, generation,
augmentation, and result synchronization.

The full product requirements, architecture, module plan, permissions, memory
design, and Harness activation standards are in `docs/PRD.md`.

## Current status

This repository currently contains two extracted Lao adapters and the durable
planning artifacts for the larger Agent:

- `lao-quality-review`: two-stage question classification and quality review.
- `lao-difficulty-prescreen`: one GPT-5.5 answer and one Qwen answer used only
  as a coarse difficulty screen.

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
implemented and cannot emit `FINAL_ACCEPTED`. The renamed prescreen manifest is
also temporarily disabled until its fresh real-API verification is complete.

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

Run the prescreen after its manifest is verified and enabled:

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
