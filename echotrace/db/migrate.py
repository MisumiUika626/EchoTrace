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
