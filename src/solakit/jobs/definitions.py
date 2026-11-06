"""Authoritative job definitions in app.db, separate from rebuildable indexes."""

import sqlite3

from solakit.core.config import Job


class Definitions:
    def __init__(self, store):
        self.store = store

    def connect(self):
        return sqlite3.connect(self.store.root / "app.db")

    def load(self, seeds, *, validate=None):
        with self.store.lock, self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS job_definitions "
                "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, deleted INTEGER NOT NULL)"
            )
            for job in seeds:
                if (
                    validate
                    and not db.execute(
                        "SELECT 1 FROM job_definitions WHERE id=?", (job.id,)
                    ).fetchone()
                ):
                    validate(job)
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


def inspect_definitions(path, seeds):
    """Read authoritative definitions without importing seeds or mutating storage."""
    jobs = {job.id: job for job in seeds}
    if not path.exists():
        return list(jobs.values())
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='job_definitions'"
        ).fetchone():
            for identity, payload, deleted in db.execute(
                "SELECT id, payload, deleted FROM job_definitions"
            ):
                jobs.pop(identity, None)
                if not deleted:
                    jobs[identity] = Job.model_validate_json(payload)
    return list(jobs.values())
