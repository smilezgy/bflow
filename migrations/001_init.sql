-- Bflow 初始表结构
-- 见 spec/04-data-model.md §2

CREATE TABLE IF NOT EXISTS channel (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    protocol      TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    config_json   TEXT NOT NULL DEFAULT '{}',
    credential_id TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_credential (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'bearer',
    secret_enc BLOB NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_capability (
    channel_id      TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'default',
    verified_at     REAL,
    PRIMARY KEY (channel_id, model)
);

CREATE TABLE IF NOT EXISTS asset (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    format     TEXT NOT NULL DEFAULT '',
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT,
    origin     TEXT NOT NULL DEFAULT 'upload',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_created ON asset(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_sha ON asset(sha256);
CREATE INDEX IF NOT EXISTS idx_asset_kind ON asset(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS task (
    id               TEXT PRIMARY KEY,
    channel_id       TEXT NOT NULL,
    model            TEXT NOT NULL,
    operation        TEXT NOT NULL DEFAULT 'generate',
    status           TEXT NOT NULL DEFAULT 'pending',
    prompt           TEXT NOT NULL DEFAULT '',
    request_json     TEXT NOT NULL DEFAULT '{}',
    requested_size   TEXT,
    requested_format TEXT,
    actual_size      TEXT,
    actual_format    TEXT,
    error_code       TEXT,
    error_message    TEXT,
    warnings_json    TEXT NOT NULL DEFAULT '[]',
    progress         REAL NOT NULL DEFAULT 0,
    upstream_task_id TEXT,
    billed           INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_task_created ON task(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
CREATE INDEX IF NOT EXISTS idx_task_channel ON task(channel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS task_asset (
    task_id  TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    role     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, asset_id, role)
);
CREATE INDEX IF NOT EXISTS idx_task_asset_asset ON task_asset(asset_id);

CREATE TABLE IF NOT EXISTS canvas (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '未命名画布',
    path         TEXT NOT NULL,
    thumbnail_id TEXT REFERENCES asset(id) ON DELETE SET NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canvas_updated ON canvas(updated_at DESC);

CREATE TABLE IF NOT EXISTS canvas_node (
    id         TEXT PRIMARY KEY,
    canvas_id  TEXT NOT NULL REFERENCES canvas(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    x          REAL NOT NULL DEFAULT 0,
    y          REAL NOT NULL DEFAULT 0,
    width      REAL NOT NULL DEFAULT 320,
    height     REAL NOT NULL DEFAULT 320,
    z_index    INTEGER NOT NULL DEFAULT 0,
    data_json  TEXT NOT NULL DEFAULT '{}',
    asset_id   TEXT REFERENCES asset(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_canvas ON canvas_node(canvas_id);

CREATE TABLE IF NOT EXISTS canvas_edge (
    id        TEXT PRIMARY KEY,
    canvas_id TEXT NOT NULL REFERENCES canvas(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES canvas_node(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES canvas_node(id) ON DELETE CASCADE,
    label     TEXT,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edge_canvas ON canvas_edge(canvas_id);

CREATE TABLE IF NOT EXISTS asset_tag (
    asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (asset_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tag ON asset_tag(tag);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS update_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    current_version  TEXT,
    remote_version   TEXT,
    last_checked_at  REAL,
    backup_path      TEXT,
    pending_rollback INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO update_state (id, current_version) VALUES (1, '2026.11.17');
