"""Append-only SQLite journal; replay follows ingestion ID, never exchange timestamps."""

import json
from pathlib import Path
import sqlite3
from .models import Event


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, received_ms INTEGER NOT NULL, venue TEXT NOT NULL,
          symbol TEXT NOT NULL, kind TEXT NOT NULL, document TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_market ON events(venue,symbol,id);
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY, event_id INTEGER, document TEXT NOT NULL);
        """)
        self.db.commit()
        self.pending = 0

    def append(self, event):
        cursor = self.db.execute(
            "INSERT INTO events(received_ms,venue,symbol,kind,document) VALUES(?,?,?,?,?)",
            (
                event.received_ms,
                event.venue,
                event.symbol,
                event.kind,
                json.dumps(event.record(), separators=(",", ":"), allow_nan=False),
            ),
        )
        self.pending += 1
        return cursor.lastrowid

    def signal(self, event_id, signal):
        self.db.execute(
            "INSERT INTO signals(event_id,document) VALUES(?,?)",
            (event_id, json.dumps(signal.record(), allow_nan=False)),
        )

    def flush(self):
        self.db.commit()
        self.pending = 0

    def events(self):
        for (document,) in self.db.execute("SELECT document FROM events ORDER BY id"):
            yield Event(**json.loads(document))

    def export(self, path):
        with open(path, "w", encoding="utf-8") as output:
            for e in self.events():
                output.write(json.dumps(e.record(), allow_nan=False) + "\n")

    def close(self):
        self.flush()
        self.db.close()


def read_events(path):
    if str(path).endswith((".sqlite", ".db", ".sqlite3")):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            for row in db.execute("SELECT document FROM events ORDER BY id"):
                yield Event(**json.loads(row[0]))
    else:
        with open(path, encoding="utf-8-sig") as source:
            for line in source:
                if line.strip():
                    yield Event(**json.loads(line))
