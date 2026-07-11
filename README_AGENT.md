# Agent operating notes

Before changing this repository, read these sources of truth in order:

1. `docs/PRD.md`
2. `feature_list.json`
3. `progress.md`
4. `configs/question_pipeline.yaml`

Choose only one unblocked feature, mark it `in_progress`, implement and verify
it, then update both `feature_list.json` and `progress.md`. A feature is not
`done` merely because code exists; its recorded acceptance and verification
must pass.

The canonical review order is:

```text
quality_review
-> difficulty_prescreen
-> consistency_review
-> answer_synthesis
-> qwen_passrate_review
-> final_gate
```

Never skip, reorder, or silently treat a disabled placeholder as passed. If a
required stage is missing or disabled, the full pipeline must return
`BLOCKED_NOT_IMPLEMENTED`. Only `final_gate` may emit `FINAL_ACCEPTED`.

`lao_difficulty_prescreen` is a one-shot coarse screen. `HARD` may pass to
consistency review, `EASY` is rejected as too easy, and errors are quarantined.
Its `passrate` must always be `null`; never derive passrate from its Qwen score.

The `laokuoyang` source remains an external, local legacy dependency. Do not
copy its data, prompts, credentials, or generated artifacts into this public
repository without an explicit publication decision.

Before enabling a tool, run:

```powershell
python -m pytest
```

The durable runtime entrypoint is `data-agent`. `preflight` and `status` must be
read-only; `run` must reject a disabled complete pipeline before creating a
database. Local state mutations use SQLite transactions, CAS state versions,
fencing leases, immutable content-addressed artifacts, and a deduplicated
Outbox. Never bypass those primitives with ad-hoc JSON progress files.

Real model smoke tests must use tiny synthetic fixtures, bounded concurrency,
and a separate run directory. Never print API keys or copy `.env` files.

Runtime permissions are capability based. Model-facing nodes do not receive a
general shell, arbitrary filesystem access, or direct Feishu write access.
Feishu writes must go through an allowlisted Outbox and must be read back for
reconciliation. Tool errors, missing artifacts, invalid schemas, and uncertain
results fail closed; there is no human-wait state.

Do not change a manifest to `enabled: true` until its contract, offline tests,
failure/recovery tests, bounded real smoke, idempotency check, and secret scan
all have evidence in `progress.md`.

At every handoff append: completed work, changed files, exact verification,
blockers, and the next recommended feature. Preserve old evidence as history;
do not rewrite it to make the current state look greener than it is.
