CREATE TABLE delivery_cooldowns (
    destination TEXT PRIMARY KEY,
    until REAL NOT NULL
);
CREATE TABLE checks (
    destination TEXT NOT NULL,
    client_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (destination, client_id, server_id)
);
CREATE TABLE incidents (
    id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    client_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    recovered INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    delivered_revision INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX active_incident ON incidents(destination, client_id, server_id)
    WHERE recovered = 0;
PRAGMA user_version = 1;
