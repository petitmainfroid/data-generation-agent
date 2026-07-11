CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    checksum TEXT NOT NULL CHECK (
        length(checksum) = 64
        AND checksum NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY CHECK (length(trim(job_id)) > 0),
    job_type TEXT NOT NULL CHECK (length(trim(job_type)) > 0),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'PAUSED', 'CANCEL_REQUESTED', 'CANCELLED',
        'COMPLETED', 'COMPLETED_PARTIAL', 'FAILED', 'BLOCKED',
        'BLOCKED_NOT_IMPLEMENTED'
    )),
    policy_digest TEXT NOT NULL CHECK (
        length(policy_digest) = 64
        AND policy_digest NOT GLOB '*[^0-9a-f]*'
    ),
    config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY CHECK (length(trim(candidate_id)) > 0),
    job_id TEXT NOT NULL,
    source_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'ACCEPTED', 'REJECTED', 'QUARANTINED',
        'FAILED', 'CANCELLED', 'QUEUED', 'GENERATING', 'QUALITY_REVIEW',
        'DIFFICULTY_PRESCREEN', 'CONSISTENCY_REVIEW', 'ANSWER_SYNTHESIS',
        'QWEN_PASSRATE_REVIEW', 'FINAL_GATE', 'REPAIRING', 'RETRY_WAIT',
        'FINAL_ACCEPTED', 'REJECTED_QUALITY', 'REJECTED_COARSE_POLICY',
        'REJECTED_INCONSISTENT', 'REJECTED_PASSRATE', 'REJECTED_DUPLICATE',
        'QUARANTINED_UNCERTAIN', 'QUARANTINED_DISAGREEMENT',
        'QUARANTINED_TOOL_ERROR', 'BUDGET_EXHAUSTED'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
    UNIQUE (job_id, source_id)
);

CREATE TABLE revisions (
    revision_id TEXT PRIMARY KEY CHECK (length(trim(revision_id)) > 0),
    candidate_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    parent_revision_id TEXT,
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT', 'ACTIVE', 'SUPERSEDED', 'ACCEPTED', 'REJECTED',
        'QUARANTINED', 'FAILED'
    )),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_revision_id) REFERENCES revisions(revision_id) ON DELETE RESTRICT,
    UNIQUE (candidate_id, revision_number),
    UNIQUE (candidate_id, content_hash)
);

CREATE UNIQUE INDEX revisions_one_active_per_candidate
    ON revisions(candidate_id)
    WHERE status = 'ACTIVE';

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY CHECK (length(trim(attempt_id)) > 0),
    revision_id TEXT NOT NULL,
    stage_id TEXT NOT NULL CHECK (length(trim(stage_id)) > 0),
    tool_id TEXT NOT NULL CHECK (length(trim(tool_id)) > 0),
    tool_version TEXT NOT NULL CHECK (length(trim(tool_version)) > 0),
    input_fingerprint TEXT NOT NULL CHECK (
        length(input_fingerprint) = 64
        AND input_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_ERROR',
        'TERMINAL_ERROR', 'CANCELLED', 'STALE', 'LEASED', 'COMPLETED',
        'INVALID', 'RETRY_WAIT', 'FAILED'
    )),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    provider_request_id TEXT,
    result_id TEXT UNIQUE,
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (revision_id) REFERENCES revisions(revision_id) ON DELETE RESTRICT,
    UNIQUE (revision_id, stage_id, input_fingerprint, attempt_number)
);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE CHECK (length(trim(event_key)) > 0),
    job_id TEXT,
    aggregate_type TEXT NOT NULL CHECK (length(trim(aggregate_type)) > 0),
    aggregate_id TEXT NOT NULL CHECK (length(trim(aggregate_id)) > 0),
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    causation_id TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
);

CREATE TRIGGER events_reject_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_reject_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY CHECK (length(trim(artifact_id)) > 0),
    job_id TEXT,
    revision_id TEXT,
    attempt_id TEXT,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    sha256 TEXT NOT NULL UNIQUE CHECK (
        length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    uri TEXT NOT NULL UNIQUE CHECK (length(trim(uri)) > 0),
    media_type TEXT NOT NULL CHECK (length(trim(media_type)) > 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
    FOREIGN KEY (revision_id) REFERENCES revisions(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id) ON DELETE RESTRICT
);

CREATE TRIGGER artifacts_reject_update
BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TRIGGER artifacts_reject_delete
BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TABLE leases (
    resource_type TEXT NOT NULL CHECK (length(trim(resource_type)) > 0),
    resource_id TEXT NOT NULL CHECK (length(trim(resource_id)) > 0),
    owner_id TEXT NOT NULL CHECK (length(trim(owner_id)) > 0),
    lease_token TEXT NOT NULL UNIQUE CHECK (length(trim(lease_token)) > 0),
    acquired_at TEXT NOT NULL CHECK (length(trim(acquired_at)) > 0),
    heartbeat_at TEXT NOT NULL CHECK (length(trim(heartbeat_at)) > 0),
    expires_at TEXT NOT NULL CHECK (length(trim(expires_at)) > 0),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    PRIMARY KEY (resource_type, resource_id)
);

CREATE TABLE budgets (
    job_id TEXT NOT NULL,
    budget_type TEXT NOT NULL CHECK (length(trim(budget_type)) > 0),
    unit TEXT NOT NULL CHECK (length(trim(unit)) > 0),
    limit_amount REAL NOT NULL CHECK (limit_amount >= 0),
    reserved_amount REAL NOT NULL DEFAULT 0 CHECK (reserved_amount >= 0),
    consumed_amount REAL NOT NULL DEFAULT 0 CHECK (consumed_amount >= 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    PRIMARY KEY (job_id, budget_type),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
    CHECK (reserved_amount + consumed_amount <= limit_amount)
);

CREATE TABLE trials (
    trial_id TEXT PRIMARY KEY CHECK (length(trim(trial_id)) > 0),
    attempt_id TEXT NOT NULL,
    trial_index INTEGER NOT NULL CHECK (trial_index >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'COMPLETED', 'INVALID', 'ERROR', 'CANCELLED'
    )),
    valid INTEGER CHECK (valid IS NULL OR valid IN (0, 1)),
    passed INTEGER CHECK (passed IS NULL OR passed IN (0, 1)),
    result_artifact_id TEXT,
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY (result_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    UNIQUE (attempt_id, trial_index),
    CHECK (passed IS NULL OR passed = 0 OR valid = 1),
    CHECK (status != 'COMPLETED' OR (valid = 1 AND passed IS NOT NULL)),
    CHECK (status != 'INVALID' OR valid = 0)
);

CREATE TABLE outbox (
    outbox_id TEXT PRIMARY KEY CHECK (length(trim(outbox_id)) > 0),
    job_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL CHECK (length(trim(aggregate_type)) > 0),
    aggregate_id TEXT NOT NULL CHECK (length(trim(aggregate_id)) > 0),
    destination TEXT NOT NULL CHECK (length(trim(destination)) > 0),
    operation TEXT NOT NULL CHECK (operation IN ('CREATE', 'UPSERT', 'PATCH')),
    dedupe_key TEXT NOT NULL UNIQUE CHECK (length(trim(dedupe_key)) > 0),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash) = 64
        AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    payload_artifact_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'IN_FLIGHT', 'RETRY_WAIT', 'SENT', 'FAILED', 'CANCELLED'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at TEXT NOT NULL CHECK (length(trim(available_at)) > 0),
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    sent_at TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    CHECK ((status = 'SENT') = (sent_at IS NOT NULL)),
    CHECK (
        status != 'IN_FLIGHT'
        OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX jobs_by_status ON jobs(status, updated_at);
CREATE INDEX candidates_by_job_status ON candidates(job_id, status);
CREATE INDEX revisions_by_candidate ON revisions(candidate_id, revision_number);
CREATE INDEX attempts_by_revision_stage ON attempts(revision_id, stage_id, status);
CREATE INDEX events_by_aggregate ON events(aggregate_type, aggregate_id, event_id);
CREATE INDEX events_by_job ON events(job_id, event_id);
CREATE INDEX artifacts_by_job ON artifacts(job_id, created_at);
CREATE INDEX artifacts_by_revision ON artifacts(revision_id, created_at);
CREATE INDEX leases_by_expiry ON leases(expires_at);
CREATE INDEX trials_by_attempt_status ON trials(attempt_id, status, trial_index);
CREATE INDEX outbox_ready ON outbox(status, available_at);
CREATE INDEX outbox_by_job ON outbox(job_id, status, created_at);

CREATE TRIGGER schema_migrations_reject_update
BEFORE UPDATE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'migration history is immutable');
END;

CREATE TRIGGER schema_migrations_reject_delete
BEFORE DELETE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'migration history is immutable');
END;
