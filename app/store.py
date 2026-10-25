"""Persistent queue and library; every method uses a short SQLite connection."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    lyrics TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    input_lyrics TEXT NOT NULL,
    formatted_lyrics TEXT NOT NULL,
    generated_lyrics TEXT NOT NULL DEFAULT '',
    vocal_mode TEXT NOT NULL DEFAULT 'auto',
    lyric_language TEXT NOT NULL DEFAULT 'en',
    negative_prompt TEXT NOT NULL DEFAULT '',
    requested_bpm INTEGER,
    requested_duration INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    model TEXT NOT NULL,
    lm_model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    prepared_prompt TEXT NOT NULL DEFAULT '',
    ace_payload TEXT,
    lyrics_validation TEXT,
    preparation_attempts INTEGER NOT NULL DEFAULT 0,
    result_audio_url TEXT,
    ace_task_id TEXT,
    wav_path TEXT,
    source_path TEXT,
    mp3_path TEXT,
    error TEXT,
    ace_stage TEXT,
    ace_progress REAL,
    ace_progress_text TEXT,
    last_checked_at TEXT,
    cover_status TEXT NOT NULL DEFAULT 'none',
    cover_prompt TEXT,
    cover_path TEXT,
    cover_error TEXT,
    deleted_at TEXT,
    favorite INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tracks_queue ON tracks(status, created_at, position);
CREATE INDEX IF NOT EXISTS idx_tracks_batch ON tracks(batch_id, position);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO app_state(key,value) VALUES('queue_paused','0');
INSERT OR IGNORE INTO app_state(key,value) VALUES('ace_completions_since_maintenance','0');
CREATE TABLE IF NOT EXISTS notifications (
    batch_id TEXT NOT NULL REFERENCES batches(id),
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(batch_id, kind)
);
CREATE TABLE IF NOT EXISTS long_projects (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE REFERENCES batches(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    total_duration INTEGER NOT NULL,
    segment_duration INTEGER NOT NULL,
    crossfade REAL NOT NULL,
    evolution TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'generating',
    wav_path TEXT,
    mp3_path TEXT,
    flac_path TEXT,
    error TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

ACTIVE_STATUSES = "'draft','preparing','ready','submitting','generating','post_processing'"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
            for name, kind in (("ace_stage", "TEXT"), ("ace_progress", "REAL"),
                               ("ace_progress_text", "TEXT"), ("last_checked_at", "TEXT"),
                               ("vocal_mode", "TEXT NOT NULL DEFAULT 'auto'"),
                               ("lyric_language", "TEXT NOT NULL DEFAULT 'en'"),
                               ("negative_prompt", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {kind}")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
            for name, kind in (("prepared_prompt", "TEXT NOT NULL DEFAULT ''"), ("ace_payload", "TEXT"),
                               ("lyrics_validation", "TEXT"),
                               ("preparation_attempts", "INTEGER NOT NULL DEFAULT 0"),
                               ("result_audio_url", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {kind}")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
            if "source_path" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN source_path TEXT")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
            for name, kind in (("cover_status", "TEXT NOT NULL DEFAULT 'none'"),
                               ("cover_prompt", "TEXT"), ("cover_path", "TEXT"),
                               ("cover_error", "TEXT"), ("deleted_at", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {kind}")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
            if "long_project_id" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN long_project_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_long_project ON tracks(long_project_id, position)")
            long_columns = {row["name"] for row in conn.execute("PRAGMA table_info(long_projects)")}
            if "deleted_at" not in long_columns:
                conn.execute("ALTER TABLE long_projects ADD COLUMN deleted_at TEXT")
            conn.execute("UPDATE long_projects SET status='generating' WHERE status='assembling'")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create_batch(self, description: str, lyrics: str, formatted: str, bpm: int | None,
                     count: int, duration: int, model: str, lm_model: str, seeds: list[int], title: str,
                     webhook_url: str = "", push_url: str = "", vocal_mode: str = "auto",
                     lyric_language: str = "en", negative_prompt: str = "",
                     generate_covers: bool = False):
        batch_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("INSERT INTO batches(id,description,lyrics) VALUES(?,?,?)", (batch_id, description, lyrics))
            for kind, url in (("webhook", webhook_url), ("push", push_url)):
                if url:
                    conn.execute("INSERT INTO notifications(batch_id,kind,url) VALUES(?,?,?)", (batch_id, kind, url))
            for position in range(1, count + 1):
                track_id = str(uuid.uuid4())
                conn.execute("""INSERT INTO tracks
                    (id,batch_id,position,title,description,input_lyrics,formatted_lyrics,vocal_mode,lyric_language,negative_prompt,cover_status,
                     requested_bpm,requested_duration,seed,model,lm_model,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (track_id, batch_id, position, f"{title} · {position:02d}", description,
                     lyrics, formatted, vocal_mode, lyric_language, negative_prompt,
                     "queued" if generate_covers else "none", bpm, duration,
                     seeds[position - 1], model, lm_model, "draft"))
        return batch_id

    def create_long_project(self, title: str, description: str, total_duration: int,
                            segment_duration: int, crossfade: float, evolution: str,
                            prompts: list[str], bpm: int | None, model: str, lm_model: str,
                            seeds: list[int], negative_prompt: str) -> str:
        project_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("INSERT INTO batches(id,description,lyrics) VALUES(?,?,'')", (batch_id, description))
            conn.execute("""INSERT INTO long_projects
                (id,batch_id,title,description,total_duration,segment_duration,crossfade,evolution)
                VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, batch_id, title, description, total_duration,
                 segment_duration, crossfade, evolution))
            for position, prompt in enumerate(prompts, 1):
                conn.execute("""INSERT INTO tracks
                    (id,batch_id,long_project_id,position,title,description,input_lyrics,
                     formatted_lyrics,vocal_mode,lyric_language,negative_prompt,cover_status,
                     requested_bpm,requested_duration,seed,model,lm_model,status)
                    VALUES(?,?,?,?,?,?, '', '', 'instrumental','en',?,'none',?,?,?,?,?,'draft')""",
                    (str(uuid.uuid4()), batch_id, project_id, position,
                     f"{title} · segment {position:02d}", prompt, negative_prompt,
                     bpm, segment_duration, seeds[position - 1], model, lm_model))
        return project_id

    def tracks(self, query: str = "", favorite: bool = False, limit: int = 200, offset: int = 0):
        sql = """SELECT id,batch_id,position,title,SUBSTR(description,1,180) AS description,
            requested_bpm,requested_duration,vocal_mode,lyric_language,status,error,favorite,created_at,updated_at,
            ace_stage,ace_progress,last_checked_at,cover_status,cover_path,cover_error,
            (SELECT COUNT(*) FROM tracks siblings WHERE siblings.batch_id=tracks.batch_id
             AND siblings.long_project_id IS NULL) AS batch_size
            FROM tracks WHERE deleted_at IS NULL AND long_project_id IS NULL"""
        params: list[object] = []
        if query:
            sql += " AND (title LIKE ? OR description LIKE ? OR input_lyrics LIKE ? OR generated_lyrics LIKE ?)"
            params += [f"%{query}%"] * 4
        if favorite:
            sql += " AND favorite=1"
        sql += " ORDER BY created_at DESC, position DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def track_count(self, query: str = "", favorite: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL AND long_project_id IS NULL"
        params: list[object] = []
        if query:
            sql += " AND (title LIKE ? OR description LIKE ? OR input_lyrics LIKE ? OR generated_lyrics LIKE ?)"
            params += [f"%{query}%"] * 4
        if favorite:
            sql += " AND favorite=1"
        with self.connection() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def batch_tracks(self, batch_id: str):
        sql = """SELECT id,batch_id,position,title,SUBSTR(description,1,180) AS description,
            requested_bpm,requested_duration,vocal_mode,lyric_language,status,error,favorite,created_at,updated_at,
            ace_stage,ace_progress,last_checked_at,cover_status,cover_path,cover_error,
            (SELECT COUNT(*) FROM tracks siblings WHERE siblings.batch_id=tracks.batch_id
             AND siblings.long_project_id IS NULL) AS batch_size
            FROM tracks WHERE batch_id=? AND deleted_at IS NULL AND long_project_id IS NULL
            ORDER BY position"""
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, (batch_id,))]

    def grouped_tracks(self, query: str = "", favorite: bool = False,
                       limit: int = 20, offset: int = 0):
        matched = self.tracks(query, favorite, limit=1_000_000)
        units = []
        seen = set()
        for track in matched:
            key = ("batch", track["batch_id"]) if track["batch_size"] > 2 else ("track", track["id"])
            if key not in seen:
                seen.add(key)
                units.append(key)
        selected = units[offset:offset + limit]
        result = []
        matched_by_id = {track["id"]: track for track in matched}
        for kind, identifier in selected:
            if kind == "batch":
                result.extend(self.batch_tracks(identifier))
            else:
                result.append(matched_by_id[identifier])
        return result, len(units)

    def get(self, track_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            return dict(row) if row else None

    def soft_delete(self, track_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(f"""UPDATE tracks SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND deleted_at IS NULL AND status NOT IN ({ACTIVE_STATUSES})
                AND cover_status NOT IN ('queued','running')""", (track_id,))
            return cursor.rowcount == 1

    def soft_delete_many(self, track_ids: list[str]) -> int:
        if not track_ids:
            return 0
        placeholders = ",".join("?" for _ in track_ids)
        with self.connection() as conn:
            cursor = conn.execute(f"""UPDATE tracks SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id IN ({placeholders}) AND deleted_at IS NULL AND status NOT IN ({ACTIVE_STATUSES})
                AND cover_status NOT IN ('queued','running')""", track_ids)
            return cursor.rowcount

    def next_job(self):
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM tracks WHERE status IN ('draft','ready')
                ORDER BY created_at, position LIMIT 1""").fetchone()
            if not row:
                return None
            status = "preparing" if row["status"] == "draft" else "submitting"
            conn.execute("UPDATE tracks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, row["id"]))
            claimed = dict(row)
            claimed["status"] = status
            return claimed

    def running_job(self):
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM tracks
                WHERE status IN ('preparing','submitting','generating','post_processing')
                ORDER BY created_at LIMIT 1""").fetchone()
            return dict(row) if row else None

    def recover(self):
        # Resume durable stages. Preparation and an unconfirmed submission are safe to restart.
        with self.connection() as conn:
            conn.execute("UPDATE tracks SET status='draft' WHERE status='preparing'")
            conn.execute("UPDATE tracks SET status='ready' WHERE status='submitting' AND ace_task_id IS NULL")
            conn.execute("UPDATE tracks SET status='generating' WHERE status='submitting' AND ace_task_id IS NOT NULL")
            conn.execute("UPDATE tracks SET status='draft' WHERE status='queued'")
            conn.execute("UPDATE tracks SET status=CASE WHEN ace_task_id IS NULL THEN 'draft' ELSE 'generating' END WHERE status='running'")

    def update(self, track_id: str, **fields):
        allowed = {"title", "favorite", "status", "ace_task_id", "wav_path", "source_path", "mp3_path", "error",
                   "generated_lyrics", "ace_stage", "ace_progress", "ace_progress_text", "last_checked_at",
                   "cover_status", "cover_prompt", "cover_path", "cover_error", "formatted_lyrics",
                   "prepared_prompt", "ace_payload", "lyrics_validation", "preparation_attempts",
                   "result_audio_url", "negative_prompt"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid update fields")
        assignment = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(f"UPDATE tracks SET {assignment}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         [*fields.values(), track_id])

    def counts(self):
        with self.connection() as conn:
            return {row["status"]: row["count"] for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM tracks GROUP BY status")}

    def cover_counts(self):
        with self.connection() as conn:
            return {row["cover_status"]: row["count"] for row in conn.execute(
                "SELECT cover_status, COUNT(*) AS count FROM tracks WHERE cover_status!='none' GROUP BY cover_status")}

    def cancel_queued(self):
        with self.connection() as conn:
            cursor = conn.execute("""UPDATE tracks SET status='canceled', updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('draft','ready')""")
            return cursor.rowcount

    def batch_counts(self, batch_id: str):
        with self.connection() as conn:
            return {row["status"]: row["count"] for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM tracks WHERE batch_id=? GROUP BY status", (batch_id,))}

    def get_batch(self, batch_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT id,description,created_at FROM batches WHERE id=?", (batch_id,)).fetchone()
            return dict(row) if row else None

    def finished_batches(self, limit: int = 100):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT b.id, b.description, b.created_at,
                COUNT(t.id) AS total,
                SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN t.status='canceled' THEN 1 ELSE 0 END) AS canceled
                FROM batches b JOIN tracks t ON t.batch_id=b.id
                GROUP BY b.id HAVING SUM(CASE WHEN t.status IN ('draft','preparing','ready','submitting','generating','post_processing') THEN 1 ELSE 0 END)=0
                ORDER BY b.created_at DESC LIMIT ?""", (limit,))]

    def pending_notifications(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT n.batch_id, n.kind, n.url, n.attempts,
                b.description, COUNT(t.id) AS total,
                SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN t.status='canceled' THEN 1 ELSE 0 END) AS canceled
                FROM notifications n JOIN batches b ON b.id=n.batch_id JOIN tracks t ON t.batch_id=b.id
                WHERE n.delivered_at IS NULL AND n.next_attempt_at <= CURRENT_TIMESTAMP
                GROUP BY n.batch_id,n.kind
                HAVING SUM(CASE WHEN t.status IN ('draft','preparing','ready','submitting','generating','post_processing') THEN 1 ELSE 0 END)=0""")]

    def notification_delivered(self, batch_id: str, kind: str):
        with self.connection() as conn:
            conn.execute("UPDATE notifications SET delivered_at=CURRENT_TIMESTAMP WHERE batch_id=? AND kind=?",
                         (batch_id, kind))

    def notification_retry(self, batch_id: str, kind: str):
        with self.connection() as conn:
            conn.execute("""UPDATE notifications SET attempts=attempts+1,
                next_attempt_at=datetime('now', '+' || MIN(3600, 30 * (1 << MIN(attempts, 7))) || ' seconds')
                WHERE batch_id=? AND kind=?""", (batch_id, kind))

    def paused(self) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT value FROM app_state WHERE key='queue_paused'").fetchone()[0] == "1"

    def set_paused(self, paused: bool):
        with self.connection() as conn:
            conn.execute("UPDATE app_state SET value=? WHERE key='queue_paused'", ("1" if paused else "0",))

    def record_ace_completion(self) -> int:
        with self.connection() as conn:
            conn.execute("""INSERT INTO app_state(key,value)
                VALUES('ace_completions_since_maintenance','1')
                ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1""")
            return int(conn.execute("SELECT value FROM app_state WHERE key='ace_completions_since_maintenance'")
                       .fetchone()[0])

    def ace_completions_since_maintenance(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key='ace_completions_since_maintenance'").fetchone()
            return int(row[0]) if row else 0

    def reset_ace_maintenance_counter(self):
        with self.connection() as conn:
            conn.execute("""INSERT INTO app_state(key,value)
                VALUES('ace_completions_since_maintenance','0')
                ON CONFLICT(key) DO UPDATE SET value='0'""")

    def next_cover_job(self):
        with self.connection() as conn:
            audio_work = conn.execute("""SELECT 1 FROM tracks WHERE status IN
                ('draft','preparing','ready','submitting','generating','post_processing') LIMIT 1""").fetchone()
            if audio_work:
                return None
            row = conn.execute("""SELECT * FROM tracks WHERE status='completed' AND cover_status='queued'
                ORDER BY created_at, position LIMIT 1""").fetchone()
            if not row:
                return None
            conn.execute("UPDATE tracks SET cover_status='running', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            return dict(row)

    def long_projects(self, limit: int = 20):
        with self.connection() as conn:
            conn.execute("""UPDATE long_projects SET status='failed', error='Un ou plusieurs segments ont échoué',
                updated_at=CURRENT_TIMESTAMP WHERE status='generating' AND EXISTS (
                SELECT 1 FROM tracks WHERE long_project_id=long_projects.id AND status IN ('failed','canceled'))""")
            return [dict(row) for row in conn.execute("""SELECT lp.*,
                COUNT(t.id) AS segment_count,
                SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed_segments,
                SUM(CASE WHEN t.status IN ('failed','canceled') THEN 1 ELSE 0 END) AS failed_segments,
                SUM(CASE WHEN t.status IN ('preparing','ready','submitting','generating','post_processing') THEN 1 ELSE 0 END) AS active_segments,
                MIN(CASE WHEN t.status IN ('preparing','ready','submitting','generating','post_processing') THEN t.id END) AS active_track_id,
                MIN(CASE WHEN t.status IN ('preparing','ready','submitting','generating','post_processing') THEN t.title END) AS active_track_title
                FROM long_projects lp JOIN tracks t ON t.long_project_id=lp.id
                WHERE lp.deleted_at IS NULL
                GROUP BY lp.id ORDER BY lp.created_at DESC LIMIT ?""", (limit,))]

    def get_long_project(self, project_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM long_projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def soft_delete_long_project(self, project_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("""UPDATE long_projects
                SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND deleted_at IS NULL AND status IN ('completed','failed')""", (project_id,))
            return cursor.rowcount == 1

    def long_project_tracks(self, project_id: str):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM tracks WHERE long_project_id=? ORDER BY position", (project_id,))]

    def next_long_project(self):
        with self.connection() as conn:
            row = conn.execute("""SELECT lp.* FROM long_projects lp
                WHERE lp.status='generating' AND lp.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.long_project_id=lp.id AND t.status!='completed')
                ORDER BY lp.created_at LIMIT 1""").fetchone()
            if not row:
                return None
            conn.execute("UPDATE long_projects SET status='assembling', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (row["id"],))
            claimed = dict(row)
            claimed["status"] = "assembling"
            return claimed

    def claim_long_project(self, project_id: str):
        with self.connection() as conn:
            row = conn.execute("""SELECT lp.* FROM long_projects lp
                WHERE lp.id=? AND lp.status='generating' AND lp.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.long_project_id=lp.id AND t.status!='completed')""",
                (project_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE long_projects SET status='assembling', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (project_id,))
            claimed = dict(row)
            claimed["status"] = "assembling"
            return claimed

    def update_long_project(self, project_id: str, **fields):
        allowed = {"status", "wav_path", "mp3_path", "flac_path", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid long project fields")
        assignment = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(f"UPDATE long_projects SET {assignment}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         [*fields.values(), project_id])

    def retry_long_project(self, project_id: str) -> int:
        with self.connection() as conn:
            cursor = conn.execute("""UPDATE tracks SET
                status=CASE WHEN ace_payload IS NULL THEN 'draft' ELSE 'ready' END,
                ace_task_id=NULL, result_audio_url=NULL, error=NULL, ace_stage=NULL,
                ace_progress=NULL, ace_progress_text=NULL, last_checked_at=NULL,
                updated_at=CURRENT_TIMESTAMP
                WHERE long_project_id=? AND status IN ('failed','canceled')""", (project_id,))
            if cursor.rowcount:
                conn.execute("UPDATE long_projects SET status='generating', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (project_id,))
            return cursor.rowcount
