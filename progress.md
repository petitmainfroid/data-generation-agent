# Progress

## Current snapshot

- The product-level source of truth is now `docs/PRD.md`.
- The selected architecture is a closed, versioned state graph with bounded
  Agentic generation/repair nodes; the production model does not receive a
  general shell, arbitrary filesystem access, or direct Feishu write access.
- The canonical review order is:

  ```text
  quality_review
  -> difficulty_prescreen
  -> consistency_review
  -> answer_synthesis
  -> qwen_passrate_review
  -> final_gate
  ```

- `lao_quality_review@1.1.0` exists as an independently tested adapter.
- `lao_difficulty_prescreen@1.1.0` is enabled after its fail-closed offline
  suite, bounded default-route real smoke, idempotency check, and secret scan.
- Consistency review, answer synthesis, real Qwen passrate, and the final gate
  are disabled placeholders. The full pipeline remains disabled and cannot
  emit `FINAL_ACCEPTED`.
- Generation, augmentation, repair, durable state, Feishu ingestion, Outbox
  synchronization, and counter reconciliation are planned but not implemented.
- The public Git repository currently contains only its initial tracked README;
  the current implementation and documents are uncommitted local changes.

## Current verification

- Baseline before the prescreen semantic rename: `python -m pytest` -> **10 passed**.
- Current offline suite after the prescreen rename:

  ```powershell
  $env:PYTHONDONTWRITEBYTECODE='1'
  python -m pytest -p no:cacheprovider
  ```

  Result recorded during implementation: **16 passed**.
- The current run must be repeated after PRD/Harness artifact synchronization;
  F006 is not complete until the final verification is recorded below.

## Historical adapter evidence

- Quality preflight reported `READY`: six active Prompt files parsed as strict
  YAML, output schemas parsed as JSON, and referenced definitions resolved.
- A two-question quality real smoke produced one ACCEPT, one REJECT, and zero
  normalized tool errors.
- The old direct Qwen endpoint timed out and was correctly represented as an
  error, not a hard question.
- A later one-question objective smoke through the previously working gateway
  completed as EASY with `passrate: null`.
- Repeated historical adapter runs produced stable result IDs, and the recorded
  smoke logs had no secret-pattern match.

This historical evidence justified the original adapter only. It does not by
itself activate the renamed `lao_difficulty_prescreen` contract or the full
pipeline.

## Known boundaries and risks

- The copied `laokuoyang` full-pipeline verifier is still red because configured
  seed and role-example data are absent. The extracted adapter tests use
  self-contained fixtures.
- The quality legacy configuration uses an HTTP endpoint. Production use needs
  an explicit trusted-private-network decision or migration to HTTPS/mTLS.
- The common subprocess runner still needs an environment allowlist, uniform
  secret redaction, disabled-manifest enforcement, and stale-content checks.
- Base table IDs, field IDs, knowledge scope, persona templates, consistency
  and synthesis programs, and Qwen passrate policy are not yet frozen.

## 2026-07-11 - PRD and Harness handoff

Completed so far:

- Added a full Chinese PRD covering requirements, current repository structure,
  architecture, permissions, memory and Prompt evolution, 19 modules, milestone
  plans, fail-closed states, and Harness activation standards.
- Expanded `feature_list.json` into a durable 20-feature implementation plan.
- Updated `README_AGENT.md` with the canonical stage order and activation rules.
- Added a machine-readable six-stage pipeline config and disabled manifests for
  the four not-yet-implemented review stages.
- Reframed the legacy one-shot comparison as a difficulty prescreen; its
  canonical manifest remains disabled.

Final verification:

- `$env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest -p no:cacheprovider`
  -> **21 passed**; final post-status-update rerun completed in **1.37s**.
- Parsed all non-run project JSON/YAML -> **4 JSON and 8 YAML valid**.
- `python -m compileall -q src tests` -> **passed**.
- PRD structure check -> **714 lines, 19 module sections, balanced code fences**.
- Harness artifact tests verify canonical stage order, fail-closed defaults,
  disabled placeholders, manifest/source alignment, feature dependencies, and
  required PRD sections.

Changed files include:

- `docs/PRD.md`
- `feature_list.json`
- `progress.md`
- `README_AGENT.md`
- `configs/question_pipeline.yaml`
- `contracts/difficulty_prescreen_result.schema.json`
- `src/data_generation_agent/tools/difficulty_prescreen.py`
- `src/data_generation_agent/tools/legacy_gap_difficulty_review.py`
- `tests/test_legacy_gap_difficulty_review.py`
- tool manifests and `pyproject.toml`

Blocked:

- F012-F014 require the user's consistency, answer-synthesis, and real
  passrate programs plus their intended contracts and policies.

Next:

1. Start F007: harden and reverify `lao_difficulty_prescreen`, run a fresh
   one-question default-gateway smoke, repeat it for idempotency, scan artifacts,
   and only then enable its canonical manifest.

## 2026-07-11 - Feishu development Base provisioned

Completed:

- Created a personal-account development Base with three tables:
  `种子题`, `候选结果`, and `流程统计`.
- Read back all real table and field IDs and stored them only in the ignored
  local profile `configs/feishu/dev_base_profile.local.json`.
- `种子题` has 12 fields. The user only needs to fill `题目`; `sft_id`, type,
  reference answer, status, encoding diagnostics, hashes, and run metadata are
  optional or Agent-owned.
- `候选结果` has 21 fields covering lineage, task mode, revision, review stages,
  synthesized answer, Qwen passrate, terminal decision, issues, and audit IDs.
- `流程统计` has 17 fields covering per-batch/mode/type/stage funnel counts.
- Added versioned safe field definitions under `configs/feishu/fields/` and a
  public usage guide at `docs/FEISHU_BASE_SCHEMA.md`.

Source inspection:

- `615.jsonl` contains 108 valid JSONL records, 108 unique `sft_id` values, no
  empty questions, and only the `sft_id/question` fields.
- Question lengths are 240-1300 characters, average 685.
- The text contains obvious mojibake and must pass an encoding-check/repair
  stage before import or publication.

Verification:

- Readback confirmed the three table names and field counts: 12 / 21 / 17.
- All created fields returned the expected Base storage types.
- No seed or result record was inserted; the Base remains clean for user input.

Next Feishu step:

- Implement F010 ingestion and then run the first controlled read-only test on
  one user-entered question. Do not test writeback until the Outbox and field
  allowlist exist.

## 2026-07-11 - F008 tool execution boundary complete

Completed:

- Replaced whole-process environment inheritance with a minimal system
  environment plus an explicit per-tool allowlist.
- Prevented arbitrary `.env` variables such as `PYTHONPATH` and unrelated
  secrets from reaching legacy subprocesses.
- Added one redaction path for stdout, stderr, raised errors, Bearer/API-key
  assignments, and common `sk-*` token shapes.
- Standardized timeout and non-zero exit failures as `LegacyProcessError`.
- The difficulty adapter now gives the legacy process an empty env-file stub;
  actual approved credentials are injected only in memory.
- Added a runtime Tool Registry that validates fixed module commands, schema
  paths, environment policies, deprecated aliases, disabled tools, placeholders,
  pipeline enablement, and path containment.
- Added fail-closed stale-result detection when a legacy row has the same ID but
  a different or missing question body.
- Bumped the quality and difficulty-prescreen tool versions to `1.1.0`.

Verification:

- Targeted F008 suite: **28 passed in 4.62s**.
- Complete suite during implementation: **33 passed in 7.13s**.
- `git diff --check`: passed; only Windows LF/CRLF conversion notices.
- Registry preflight tests prove the full pipeline remains disabled and
  placeholders/disabled tools are not callable.
- Secret scan only matched deliberately fake token fixtures in the redaction
  tests; no external credential file or local Base profile is tracked.

Next:

- F009: build the SQLite durable Harness, transactional state graph, immutable
  artifacts, leases, budgets, Outbox, and resumable CLI.

## 2026-07-11 - F007 difficulty prescreen activated

Completed:

- Ran one synthetic objective question through the hardened adapter using the
  default judge-gateway route and 32,768-token answer/Judge limits.
- The real run completed `1/1`, mapped `EASY` to `REJECT_TOO_EASY`, returned no
  error, and kept `passrate` null as required for a one-shot coarse screen.
- Repeated the exact run against the same output directory. The normalized
  `result_id` was unchanged and the legacy judgement file remained one row.
- Enabled only `lao_difficulty_prescreen@1.1.0`; the complete pipeline remains
  fail-closed because the later four stages are still disabled.

Verification:

- Targeted prescreen/security tests: **17 passed in 2.56s**.
- Runtime scan: **16 files**, **0 approved-secret value matches**, and **0
  generic token-pattern matches**.
- The real normalized result had `status=COMPLETED`, default gateway routing,
  and `gate_decision=REJECT_TOO_EASY`.

Next:

- F009: implement and fault-test the durable Harness runtime.

## 2026-07-11 - F009 durable Harness runtime complete

Completed:

- Added a packaged SQLite migration with `jobs`, `candidates`, `revisions`,
  `attempts`, append-only `events`, immutable `artifacts`, fencing `leases`,
  atomic `budgets`, resumable `trials`, and a deduplicated `outbox`.
- Added explicit state graphs and compare-and-set transitions that update
  status/version/timestamp and append the matching Event in one transaction.
- Added lease takeover with new fencing tokens, atomic budget
  reserve/consume/release, and a content-addressed Artifact Store using
  flush/fsync plus atomic rename.
- Added a frozen-payload Outbox with dedupe conflict detection, claim leases,
  retry/ACK handling, lost-ACK recovery, and recursive secret rejection.
- Added `data-agent preflight/run/status/resume/retry/cancel/reconcile`.
  Preflight and missing-database status are read-only; disabled `run` exits
  fail-closed before creating local state.

Verification:

- Full offline suite after integration: **78 passed**.
- Recovery/chaos suite covers ten restarts, state/Event rollback, confirmed
  logical-result reuse, SQLite locking, file-before-DB recovery, lost Outbox
  ACK, and stale lease tokens: **7 passed**.
- State/lease/budget suite: **11 passed**; Artifact/Outbox suite: **14 passed**;
  schema/migration suite: **6 passed**; CLI/service suite: **6 passed**.
- Built `data_generation_agent-0.1.0-py3-none-any.whl`; it contains the SQL
  migration and CLI. An isolated install initialized schema version 1 with 12
  SQLite tables, proving migrations do not depend on the source checkout.
- Real CLI preflight reported the first two stages ready and the remaining four
  `BLOCKED_NOT_IMPLEMENTED`. A real disabled `run` exited 2 and created neither
  a database nor an artifact directory.
- `git diff --check` passed during module verification; `runs/` remains ignored.

Recovery guarantee:

- Confirmed responses and logical results are not repeated after restart.
  Providers without an idempotency key can still physically bill twice if a
  process dies after the response but before durable confirmation; the Harness
  promises exactly-once logical application, not impossible network-level
  exactly-once execution.

Next:

- F010: implement registered Feishu Base snapshot ingestion and run a bounded
  read-only smoke against the personal development Base.

## 2026-07-11 - F010 ingestion implementation checkpoint

Implemented:

- Added an ignored local Base profile contract with Base/table/view/field alias
  allowlists and token-safe representations/errors.
- Added a fixed `lark-cli` read client. On Windows it invokes the installed
  Node entrypoint directly with `shell=False`, avoiding `.cmd` shell execution.
- Added strict parsing for the real columnar `record-list` envelope, field
  projection, identity, parallel-array lengths, offsets, duplicates, 429/5xx
  bounded retry, and no-progress pagination.
- Added normalization for required questions, optional user `sft_id`, content
  hashes, record revisions, encoding errors, and explicit rejected records.
- Added migration 2 for immutable source snapshots/revisions/rejections and CAS
  ingestion cursors. Each page is checkpointed as an immutable artifact before
  advancing the offset; crash/restart resumes without rereading prior pages.
- Added `data-agent ingest` and public local-profile/operation documentation.

Verification so far:

- F010 profile/client/ingestion/snapshot/runtime/schema suite: **60 passed**
  before the final boundary additions; all subsequent targeted regressions are
  also green.
- Fault tests cover 429, malformed envelopes, array-length mismatches, stale
  cursors, crash after page one, payload/relational divergence, forged content
  hashes, wrong database wiring, credential echo, and immutable history.
- Real personal-Base field read: 12 registered seed fields, identity matched,
  zero records, `has_more=false`.
- Real `data-agent ingest` produced one immutable zero-record snapshot; repeat
  returned `created=false`. Two local runtime files contained **0** matches for
  the registered token/table/field values.

Checkpoint blocker (resolved by the next entry):

- At this checkpoint the Harness still required a 3-10 record real read and the
  development seed table was empty. The following F016 entry resolves this via
  the tested field-allowlisted Outbox writer, not an ad-hoc direct write.

## 2026-07-11 - F010 and F016 live Feishu gates complete

Completed:

- Added a field-allowlisted writer for the registered seed, candidate, and
  progress tables. Attachment/auto-number/system/computed fields are not in the
  write policy.
- Added business-key idempotency (`sft_id`, `candidate_id`, `stats_key`), fixed
  shell-free `record-upsert`, post-write filtered readback, and a dispatcher
  that ACKs only verified writes.
- Added append-only stage-event counter rebuild, stable stats keys/dedupe keys,
  and `data-agent rebuild-counters` / `sync-feishu`.

Live development-Base evidence:

- Enqueued three public synthetic seeds, dispatched them serially, and read the
  source table back in two pages: **3 source / 3 accepted / 0 rejected**.
- Repeated ingestion returned the same snapshot with `created=false`.
- Wrote and read back one candidate-result record; business-key lookup returned
  exactly one matching record.
- Rebuilt one progress row from three candidate-stage events. The first remote
  progress write succeeded but strict readback rejected the platform's
  single-select array shape, leaving `RETRY_WAIT`; after type normalization the
  same Outbox was reclaimed and became `SENT` with one Base record.
- Appended a later event and rebuilt from all four events. The same stats row
  updated to `pending=0`, `running=0`, `passed=2`, `rejected=1`,
  `quarantined=0`, `machine_remaining=2`, `qualified_deficit=0`.
- Two content versions of the progress Outbox are `SENT`, while Base still has
  exactly one `stats_key` row and latest readback equals the local projection.
- Scanned five local runtime files: **0** matches for the registered Base token,
  table IDs, or field IDs.

Offline verification during implementation:

- F010 checkpoint full suite: **134 passed**.
- Dispatcher + Outbox: **23 passed**; counter projection: **9 passed**; writer,
  dispatcher, counter, and sync integration suites all pass.

Security note:

- During a separate read-only subagent diagnostic, PowerShell decoded the
  ignored local profile incorrectly and echoed its contents into an internal
  tool log. No value entered Git, Artifact, SQLite, model Prompt, or this
  document, and OAuth is still required to access the Base. Recreate/rotate the
  personal development Base identifiers before treating it as a production
  resource.

Next:

- F011: implement immutable knowledge/persona snapshots, safe retrieval, and
  the versioned Prompt compiler.
