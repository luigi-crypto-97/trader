"""Append-only, hash-chained audit log.

Every decision (signal, risk verdict, approval, order, fill, kill switch) is recorded with a
SHA-256 chain: editing or deleting a past record breaks verification of every later record.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from trader.db import AuditRow, Database, dumps

GENESIS = "0" * 64


def _digest(prev_hash: str, ts: datetime, event_type: str, payload: str) -> str:
    material = f"{prev_hash}|{ts.astimezone(UTC).isoformat()}|{event_type}|{payload}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    records: int
    first_bad_seq: int | None = None


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, event_type: str, payload: dict[str, Any], ts: datetime | None = None) -> str:
        ts = (ts or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        body = dumps(payload)
        with self.db.session() as s:
            last = s.scalars(select(AuditRow).order_by(AuditRow.seq.desc()).limit(1)).first()
            prev = last.hash if last else GENESIS
            digest = _digest(prev, ts, event_type, body)
            s.add(AuditRow(ts=ts, event_type=event_type, payload=body, prev_hash=prev, hash=digest))
        return digest

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(AuditRow).order_by(AuditRow.seq.desc()).limit(n)).all()
            return [
                {"seq": r.seq, "ts": r.ts.isoformat(), "event": r.event_type, "payload": r.payload}
                for r in reversed(rows)
            ]

    def verify(self) -> AuditVerification:
        prev = GENESIS
        count = 0
        with self.db.session() as s:
            for row in s.scalars(select(AuditRow).order_by(AuditRow.seq)):
                count += 1
                expected = _digest(prev, row.ts, row.event_type, row.payload)
                if row.prev_hash != prev or row.hash != expected:
                    return AuditVerification(ok=False, records=count, first_bad_seq=row.seq)
                prev = row.hash
        return AuditVerification(ok=True, records=count)
