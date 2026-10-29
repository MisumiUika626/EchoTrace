PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS contacts (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL DEFAULT 'qq', uid TEXT NOT NULL,
 uin TEXT, display_name TEXT NOT NULL, alias TEXT, avatar_path TEXT, remark TEXT,
 slug TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, UNIQUE(platform,uid));
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id),
 chat_type INTEGER DEFAULT 1 CHECK(chat_type=1), name TEXT, msg_count INTEGER DEFAULT 0,
 first_ts INTEGER, last_ts INTEGER, UNIQUE(contact_id,chat_type));
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), msg_id TEXT,
 sender TEXT NOT NULL CHECK(sender IN ('me','them','system')), sender_raw TEXT,
 ts INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT, reply_to TEXT, raw JSON NOT NULL,
 hash TEXT NOT NULL UNIQUE, flags JSON DEFAULT '{}', sequence INTEGER, evidence_id TEXT, source_index INTEGER);
CREATE INDEX IF NOT EXISTS messages_session_time ON messages(session_id,ts,id);
CREATE INDEX IF NOT EXISTS messages_platform_id ON messages(session_id,msg_id);
CREATE TABLE IF NOT EXISTS import_sources (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), file TEXT NOT NULL,
 sha256 TEXT NOT NULL, messages_sha256 TEXT NOT NULL, encoding TEXT, imported_at INTEGER NOT NULL,
 provenance JSON NOT NULL DEFAULT '{}',
 UNIQUE(session_id,sha256));
CREATE TABLE IF NOT EXISTS speaker_mappings (
 session_id INTEGER NOT NULL REFERENCES sessions(id), identity TEXT NOT NULL, role TEXT NOT NULL,
 PRIMARY KEY(session_id,identity));
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages',content_rowid='id',tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
 INSERT INTO messages_fts(rowid,text) VALUES(new.id,new.text); END;
CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.id,old.text); END;
CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE OF text ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.id,old.text);
 INSERT INTO messages_fts(rowid,text) VALUES(new.id,new.text); END;
CREATE TABLE IF NOT EXISTS quotes (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id), start_msg INTEGER,
 end_msg INTEGER, text TEXT, score REAL, dims JSON, source TEXT,
 embedding BLOB, evidence JSON, ts INTEGER, speaker TEXT, topic TEXT, sensitive INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS quotes_contact ON quotes(contact_id,ts);
CREATE VIRTUAL TABLE IF NOT EXISTS quotes_fts USING fts5(tokens,contact_id UNINDEXED);
CREATE TABLE IF NOT EXISTS personas (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id), version INTEGER,
 created_at INTEGER, model TEXT, thinking TEXT, token_cost INTEGER, dir TEXT, summary TEXT,
 active INTEGER DEFAULT 0, UNIQUE(contact_id,version));
CREATE TABLE IF NOT EXISTS metrics (
 id INTEGER PRIMARY KEY, contact_id INTEGER, period TEXT, key TEXT, value REAL,
 confidence REAL, anchor REAL, evidence JSON, UNIQUE(contact_id,period,key));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT, status TEXT, progress REAL, stage TEXT, payload JSON,
 result JSON, error TEXT, created_at INTEGER, updated_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_distill ON jobs(json_extract(payload,'$.contact_id'))
 WHERE kind='distill' AND status IN ('queued','running','paused');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value JSON);
CREATE TABLE IF NOT EXISTS token_usage (
 id INTEGER PRIMARY KEY, job_id TEXT, model TEXT, stage TEXT,
 prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS local_actions (
 id INTEGER PRIMARY KEY, contact_id INTEGER, action TEXT, payload JSON, created_at INTEGER);
CREATE TABLE IF NOT EXISTS conversations (
 id INTEGER PRIMARY KEY, contact_id INTEGER, role TEXT, content TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS prompt_versions (
 id INTEGER PRIMARY KEY, name TEXT, content TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS backups (
 id TEXT PRIMARY KEY, contact_id INTEGER, payload JSON, interval_hours INTEGER,
 next_run INTEGER, enabled INTEGER DEFAULT 1);
PRAGMA user_version=1;
