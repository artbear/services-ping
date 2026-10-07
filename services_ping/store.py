"""Small, locked SQLite owner.  It never silently recreates a broken database."""
from __future__ import annotations
import json, sqlite3, threading, time
from pathlib import Path
from .config import default_config, validate_config

class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir); self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.db = sqlite3.connect(self.path / "services-ping.sqlite3", check_same_thread=False, timeout=2)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,target_id TEXT NOT NULL,timestamp REAL NOT NULL,status TEXT NOT NULL,latency_ms REAL,http_status INTEGER,message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_target ON samples(target_id,id);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,timestamp REAL NOT NULL,subject TEXT NOT NULL,previous TEXT,current TEXT NOT NULL,message TEXT NOT NULL);""")
            row=self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row and row[0] != "1": raise RuntimeError("unsupported database schema")
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
            if not self.db.execute("SELECT 1 FROM config WHERE id=1").fetchone(): self.db.execute("INSERT INTO config VALUES(1,?)", (json.dumps(default_config()),))
            self.db.commit()
        except sqlite3.DatabaseError as exc: raise RuntimeError(f"cannot open database: {exc}") from exc
    def close(self):
        with self._lock: self.db.close()
    def config(self):
        with self._lock: return validate_config(json.loads(self.db.execute("SELECT body FROM config WHERE id=1").fetchone()[0]))
    def save_config(self, config):
        config = validate_config(config)
        body=json.dumps(config, ensure_ascii=False, separators=(",",":"))
        with self._lock:
            try:
                with self.db:
                    self.db.execute("UPDATE config SET body=? WHERE id=1", (body,))
                    # Cleanup belongs to the same transaction: a failed cleanup
                    # must roll back the config write before runtime publication.
                    self.delete_targets_not_in([target["id"] for target in config["targets"]])
                    for target in config["targets"]:
                        self.db.execute("DELETE FROM samples WHERE target_id=? AND id NOT IN "
                                        "(SELECT id FROM samples WHERE target_id=? ORDER BY id DESC LIMIT ?)",
                                        (target["id"], target["id"], config["settings"]["history_limit"]))
            except sqlite3.DatabaseError as exc: raise RuntimeError(f"cannot save configuration: {exc}") from exc
    def sample(self, target_id, result, limit):
        with self._lock, self.db:
            self.db.execute("INSERT INTO samples(target_id,timestamp,status,latency_ms,http_status,message) VALUES(?,?,?,?,?,?)", (target_id, result["timestamp"],result["status"],result.get("latency_ms"),result.get("http_status"),result["message"]))
            self.db.execute("DELETE FROM samples WHERE target_id=? AND id NOT IN (SELECT id FROM samples WHERE target_id=? ORDER BY id DESC LIMIT ?)", (target_id,target_id,limit))
    def history(self, target_id, limit):
        with self._lock:
            rows=self.db.execute("SELECT timestamp,status,latency_ms,http_status,message FROM samples WHERE target_id=? ORDER BY id DESC LIMIT ?", (target_id,limit)).fetchall()
        return [dict(zip(("timestamp","status","latency_ms","http_status","message"),r)) for r in reversed(rows)]
    def event(self, subject, previous, current, message):
        with self._lock, self.db:
            self.db.execute("INSERT INTO events(timestamp,subject,previous,current,message) VALUES(?,?,?,?,?)", (time.time(),subject,previous,current,message))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")
    def events(self, limit):
        with self._lock: rows=self.db.execute("SELECT timestamp,subject,previous,current,message FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("timestamp","subject","previous","current","message"),r)) for r in rows]
    def delete_targets_not_in(self, ids):
        # Caller save_config owns the transaction. No nested connection context:
        # sqlite3 nested contexts would commit the outer transaction prematurely.
        with self._lock:
            if ids:
                q=",".join("?"*len(ids)); self.db.execute(f"DELETE FROM samples WHERE target_id NOT IN ({q})", tuple(ids))
            else: self.db.execute("DELETE FROM samples")
