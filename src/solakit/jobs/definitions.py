"""Authoritative job definitions in app.db, separate from rebuildable indexes."""

import sqlite3

from solakit.core.config import Job


class Definitions:
    def __init__(self, store):
        self.store = store

    def connect(self):
        return sqlite3.connect(self.store.root / "app.db")

    def load(self, seeds):
        with self.store.lock, self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS job_definitions "
                "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, deleted INTEGER NOT NULL)"
            )
            for job in seeds:
                db.execute(
                    "INSERT OR IGNORE INTO job_definitions VALUES (?, ?, 0)",
                    (job.id, job.model_dump_json()),
                )
            return [
                Job.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM job_definitions WHERE deleted=0 ORDER BY id"
                )
            ]

    def save(self, job, *, deleted=False):
        with self.store.lock, self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO job_definitions VALUES (?, ?, ?)",
                (job.id, job.model_dump_json(), int(deleted)),
            )
