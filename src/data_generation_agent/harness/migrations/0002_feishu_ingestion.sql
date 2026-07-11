CREATE TABLE source_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK (length(trim(snapshot_id)) > 0),
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    artifact_id TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL UNIQUE CHECK (
        length(payload_hash) = 64
        AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset >= start_offset),
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    source_record_count INTEGER NOT NULL CHECK (source_record_count >= 0),
    accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    CHECK (accepted_count + rejected_count = source_record_count)
);

CREATE TABLE source_record_revisions (
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    source_record_id TEXT NOT NULL CHECK (length(trim(source_record_id)) > 0),
    record_revision TEXT NOT NULL CHECK (length(trim(record_revision)) > 0),
    base_record_id TEXT NOT NULL CHECK (length(trim(base_record_id)) > 0),
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    normalized_json TEXT NOT NULL CHECK (json_valid(normalized_json)),
    first_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (source_id, source_record_id, record_revision),
    FOREIGN KEY (first_snapshot_id) REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT
);

CREATE INDEX source_record_revisions_by_content
    ON source_record_revisions(source_id, source_record_id, content_hash);

CREATE TABLE source_rejections (
    snapshot_id TEXT NOT NULL,
    rejection_index INTEGER NOT NULL CHECK (rejection_index >= 0),
    base_record_id TEXT NOT NULL,
    source_record_id TEXT,
    record_revision TEXT,
    issue_codes_json TEXT NOT NULL CHECK (json_valid(issue_codes_json)),
    raw_content_hash TEXT NOT NULL CHECK (
        length(raw_content_hash) = 64
        AND raw_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (snapshot_id, rejection_index),
    FOREIGN KEY (snapshot_id) REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT
);

CREATE TABLE ingestion_cursors (
    source_id TEXT PRIMARY KEY CHECK (length(trim(source_id)) > 0),
    next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset >= 0),
    partial_artifact_id TEXT,
    last_snapshot_id TEXT,
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (partial_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (last_snapshot_id) REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    CHECK (next_offset = 0 OR partial_artifact_id IS NOT NULL)
);

CREATE INDEX source_snapshots_by_source
    ON source_snapshots(source_id, created_at);

CREATE TRIGGER source_snapshots_reject_update
BEFORE UPDATE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;

CREATE TRIGGER source_snapshots_reject_delete
BEFORE DELETE ON source_snapshots
BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;

CREATE TRIGGER source_record_revisions_reject_update
BEFORE UPDATE ON source_record_revisions
BEGIN
    SELECT RAISE(ABORT, 'source record revisions are immutable');
END;

CREATE TRIGGER source_record_revisions_reject_delete
BEFORE DELETE ON source_record_revisions
BEGIN
    SELECT RAISE(ABORT, 'source record revisions are immutable');
END;

CREATE TRIGGER source_rejections_reject_update
BEFORE UPDATE ON source_rejections
BEGIN
    SELECT RAISE(ABORT, 'source rejections are immutable');
END;

CREATE TRIGGER source_rejections_reject_delete
BEFORE DELETE ON source_rejections
BEGIN
    SELECT RAISE(ABORT, 'source rejections are immutable');
END;
