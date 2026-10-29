import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "echotrace.db"
        self.vector_available = False
        with self.connect() as conn:
            conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            if "revision" not in {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}:
                conn.execute("ALTER TABLE contacts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if "provenance" not in {r[1] for r in conn.execute("PRAGMA table_info(import_sources)")}:
                conn.execute("ALTER TABLE import_sources ADD COLUMN provenance JSON NOT NULL DEFAULT '{}'")
            message_columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            for column, kind in (("sequence", "INTEGER"), ("evidence_id", "TEXT"), ("source_index", "INTEGER")):
                if column not in message_columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
            for session in conn.execute(
                "SELECT DISTINCT session_id FROM messages "
                "WHERE sequence IS NULL OR evidence_id IS NULL OR source_index IS NULL"
            ):
                rows = conn.execute(
                    "SELECT id FROM messages WHERE session_id=? ORDER BY ts,id", (session[0],)
                ).fetchall()
                for sequence, row in enumerate(rows):
                    conn.execute(
                        "UPDATE messages SET sequence=COALESCE(sequence,?),evidence_id=COALESCE(evidence_id,?),"
                        "source_index=COALESCE(source_index,?),"
                        "flags=CASE WHEN ? THEN json_set(COALESCE(flags,'{}'),'$.legacy_order',1) ELSE flags END "
                        "WHERE id=?",
                        (sequence, f"m{sequence + 1:06d}", sequence, True, row[0]),
                    )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_session_sequence ON messages(session_id,sequence)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_session_evidence ON messages(session_id,evidence_id)")
            try:
                self.load_vec(conn)
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS quote_vectors USING vec0(id INTEGER PRIMARY KEY, embedding float[256])")
                self.vector_available = True
            except (ImportError, OSError, sqlite3.Error, AttributeError):
                pass

    @staticmethod
    def load_vec(conn):
        import sqlite_vec
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-16000")
        conn.execute("PRAGMA temp_store=MEMORY")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def one(self, sql, args=()):
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args).lastrowid
