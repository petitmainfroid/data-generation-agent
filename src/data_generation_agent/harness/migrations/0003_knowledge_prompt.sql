CREATE TABLE knowledge_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK (length(snapshot_id) = 64),
    source_alias TEXT NOT NULL CHECK (length(trim(source_alias)) > 0),
    source_revision TEXT NOT NULL CHECK (length(trim(source_revision)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    artifact_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    UNIQUE (source_alias, source_revision)
);

CREATE TABLE knowledge_chunks (
    chunk_id TEXT PRIMARY KEY CHECK (length(chunk_id) = 64),
    snapshot_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    citation TEXT NOT NULL UNIQUE CHECK (length(trim(citation)) > 0),
    text_hash TEXT NOT NULL CHECK (length(text_hash) = 64),
    text_content TEXT NOT NULL CHECK (length(trim(text_content)) > 0),
    token_json TEXT NOT NULL CHECK (json_valid(token_json)),
    injection_suspected INTEGER NOT NULL CHECK (injection_suspected IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES knowledge_snapshots(snapshot_id) ON DELETE RESTRICT,
    UNIQUE (snapshot_id, sequence_number)
);

CREATE TABLE persona_snapshots (
    persona_snapshot_id TEXT PRIMARY KEY CHECK (length(persona_snapshot_id) = 64),
    persona_id TEXT NOT NULL CHECK (length(trim(persona_id)) > 0),
    version TEXT NOT NULL CHECK (length(trim(version)) > 0),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    artifact_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    UNIQUE (persona_id, version)
);

CREATE TABLE prompt_compilations (
    prompt_id TEXT PRIMARY KEY CHECK (length(prompt_id) = 64),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    task_mode TEXT NOT NULL CHECK (length(trim(task_mode)) > 0),
    candidate_revision_id TEXT NOT NULL CHECK (length(trim(candidate_revision_id)) > 0),
    persona_snapshot_id TEXT NOT NULL,
    component_hashes_json TEXT NOT NULL CHECK (json_valid(component_hashes_json)),
    artifact_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (persona_snapshot_id) REFERENCES persona_snapshots(persona_snapshot_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT
);

CREATE INDEX knowledge_chunks_by_snapshot
    ON knowledge_chunks(snapshot_id, sequence_number);
CREATE INDEX prompt_compilations_by_revision
    ON prompt_compilations(candidate_revision_id, created_at);

CREATE TRIGGER knowledge_snapshots_reject_update BEFORE UPDATE ON knowledge_snapshots
BEGIN SELECT RAISE(ABORT, 'knowledge snapshots are immutable'); END;
CREATE TRIGGER knowledge_snapshots_reject_delete BEFORE DELETE ON knowledge_snapshots
BEGIN SELECT RAISE(ABORT, 'knowledge snapshots are immutable'); END;
CREATE TRIGGER knowledge_chunks_reject_update BEFORE UPDATE ON knowledge_chunks
BEGIN SELECT RAISE(ABORT, 'knowledge chunks are immutable'); END;
CREATE TRIGGER knowledge_chunks_reject_delete BEFORE DELETE ON knowledge_chunks
BEGIN SELECT RAISE(ABORT, 'knowledge chunks are immutable'); END;
CREATE TRIGGER persona_snapshots_reject_update BEFORE UPDATE ON persona_snapshots
BEGIN SELECT RAISE(ABORT, 'persona snapshots are immutable'); END;
CREATE TRIGGER persona_snapshots_reject_delete BEFORE DELETE ON persona_snapshots
BEGIN SELECT RAISE(ABORT, 'persona snapshots are immutable'); END;
CREATE TRIGGER prompt_compilations_reject_update BEFORE UPDATE ON prompt_compilations
BEGIN SELECT RAISE(ABORT, 'prompt compilations are immutable'); END;
CREATE TRIGGER prompt_compilations_reject_delete BEFORE DELETE ON prompt_compilations
BEGIN SELECT RAISE(ABORT, 'prompt compilations are immutable'); END;
