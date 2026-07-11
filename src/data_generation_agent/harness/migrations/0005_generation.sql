CREATE TABLE generation_runs (
    generation_id TEXT PRIMARY KEY CHECK (length(generation_id) = 64),
    job_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL UNIQUE,
    seed_id TEXT NOT NULL,
    persona_snapshot_id TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    model_config_hash TEXT NOT NULL CHECK (length(model_config_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('PENDING','MODEL_CONFIRMED','COMPLETED','DUPLICATE','ERROR')),
    reserved_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    model_artifact_id TEXT,
    result_artifact_id TEXT,
    provider_response_id TEXT,
    question_hash TEXT CHECK (question_hash IS NULL OR length(question_hash) = 64),
    duplicate_of_candidate_id TEXT,
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    FOREIGN KEY (persona_snapshot_id) REFERENCES persona_snapshots(persona_snapshot_id) ON DELETE RESTRICT,
    FOREIGN KEY (prompt_id) REFERENCES prompt_compilations(prompt_id) ON DELETE RESTRICT,
    FOREIGN KEY (model_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (result_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (duplicate_of_candidate_id) REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    CHECK (status != 'MODEL_CONFIRMED' OR model_artifact_id IS NOT NULL),
    CHECK (status NOT IN ('COMPLETED','DUPLICATE') OR result_artifact_id IS NOT NULL)
);

CREATE TABLE generated_question_registry (
    question_hash TEXT PRIMARY KEY CHECK (length(question_hash) = 64),
    candidate_id TEXT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL UNIQUE,
    generation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    FOREIGN KEY (revision_id) REFERENCES revisions(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id) ON DELETE RESTRICT
);

CREATE INDEX generation_runs_by_job_status ON generation_runs(job_id, status, created_at);
CREATE TRIGGER generation_runs_reject_delete BEFORE DELETE ON generation_runs
BEGIN SELECT RAISE(ABORT, 'generation runs cannot be deleted'); END;
CREATE TRIGGER generated_question_registry_reject_update BEFORE UPDATE ON generated_question_registry
BEGIN SELECT RAISE(ABORT, 'generated question registry is immutable'); END;
CREATE TRIGGER generated_question_registry_reject_delete BEFORE DELETE ON generated_question_registry
BEGIN SELECT RAISE(ABORT, 'generated question registry is immutable'); END;
