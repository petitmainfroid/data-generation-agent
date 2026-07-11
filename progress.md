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
