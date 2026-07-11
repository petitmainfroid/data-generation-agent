CREATE TABLE passrate_trials (
    trial_id TEXT PRIMARY KEY CHECK (length(trial_id) = 64),
    candidate_id TEXT NOT NULL CHECK (length(trim(candidate_id)) > 0),
    revision_id TEXT NOT NULL CHECK (length(trim(revision_id)) > 0),
    reference_answer_hash TEXT NOT NULL CHECK (length(reference_answer_hash) = 64),
    qwen_config_hash TEXT NOT NULL CHECK (length(qwen_config_hash) = 64),
    judge_config_hash TEXT NOT NULL CHECK (length(judge_config_hash) = 64),
    trial_index INTEGER NOT NULL CHECK (trial_index >= 0),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('PENDING','ANSWER_CONFIRMED','COMPLETED','ERROR')),
    qwen_artifact_id TEXT,
    judge_artifact_id TEXT,
    qwen_response_id TEXT,
    judge_response_id TEXT,
    score INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    grade TEXT,
    can_accept INTEGER CHECK (can_accept IS NULL OR can_accept IN (0,1)),
    valid INTEGER NOT NULL DEFAULT 0 CHECK (valid IN (0,1)),
    passed INTEGER CHECK (passed IS NULL OR passed IN (0,1)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (qwen_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (judge_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    UNIQUE(candidate_id, revision_id, reference_answer_hash, qwen_config_hash, judge_config_hash, trial_index),
    CHECK (status != 'ANSWER_CONFIRMED' OR qwen_artifact_id IS NOT NULL),
    CHECK (status != 'COMPLETED' OR (qwen_artifact_id IS NOT NULL AND judge_artifact_id IS NOT NULL AND valid = 1 AND passed IS NOT NULL))
);

CREATE TABLE passrate_results (
    result_id TEXT PRIMARY KEY CHECK (length(result_id) = 64),
    candidate_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    reference_answer_hash TEXT NOT NULL CHECK (length(reference_answer_hash) = 64),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash) = 64),
    requested_trials INTEGER NOT NULL CHECK (requested_trials > 0),
    completed_trials INTEGER NOT NULL CHECK (completed_trials >= 0),
    valid_trials INTEGER NOT NULL CHECK (valid_trials >= 0),
    passed_trials INTEGER NOT NULL CHECK (passed_trials >= 0),
    passrate REAL CHECK (passrate IS NULL OR passrate BETWEEN 0 AND 1),
    decision TEXT NOT NULL CHECK (decision IN ('PASS','REJECT','ERROR')),
    artifact_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    CHECK (passed_trials <= valid_trials AND valid_trials <= completed_trials AND completed_trials <= requested_trials)
);

CREATE INDEX passrate_trials_by_revision ON passrate_trials(revision_id, status, trial_index);
CREATE INDEX passrate_results_by_revision ON passrate_results(revision_id, created_at);

CREATE TRIGGER passrate_trials_reject_delete BEFORE DELETE ON passrate_trials
BEGIN SELECT RAISE(ABORT, 'passrate trials cannot be deleted'); END;
CREATE TRIGGER passrate_results_reject_update BEFORE UPDATE ON passrate_results
BEGIN SELECT RAISE(ABORT, 'passrate results are immutable'); END;
CREATE TRIGGER passrate_results_reject_delete BEFORE DELETE ON passrate_results
BEGIN SELECT RAISE(ABORT, 'passrate results are immutable'); END;
