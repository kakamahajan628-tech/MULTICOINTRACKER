"""Normalized watch rows with serialized imports; legacy JSON migration is transactional."""

from contextlib import contextmanager
import hashlib
import json

from sqlalchemy import Column, Integer, String, Table, Text, func, select


class WatchlistMixin:
    def define_wallet_table(self, metadata):
        self.wallets = Table(
            "whale_wallets",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("ordinal", Integer, nullable=False),
            Column("payload", Text, nullable=False),
        )

    @contextmanager
    def wallet_transaction(self):
        with self.engine.connect() as con:
            if self.engine.dialect.name == "sqlite":
                con.exec_driver_sql("BEGIN IMMEDIATE")
            elif self.engine.dialect.name == "postgresql":
                con.begin()
                # Shared by every watchlist writer, including offline import commands.
                con.exec_driver_sql("SELECT pg_advisory_xact_lock(735681214)")
            else:
                raise ValueError("Watchlist writes support SQLite or PostgreSQL")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    @staticmethod
    def prepare_wallet_rows(rows):
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Maximum 1,000 watch entries")
        prepared, seen = [], set()
        for row in rows:
            encoded = json.dumps(row, sort_keys=True, allow_nan=False)
            identity = row.get("id") or hashlib.sha256(encoded.encode()).hexdigest()[:24]
            if identity in seen:
                raise ValueError("Duplicate watch identity")
            seen.add(identity)
            prepared.append({"id": identity, "payload": encoded})
        return prepared

    def migrate_wallets(self):
        with self.wallet_transaction() as con:
            legacy = con.execute(
                select(self.state.c.value).where(self.state.c.key == "wallets")
            ).scalar()
            if legacy is None:
                return
            if con.execute(select(func.count()).select_from(self.wallets)).scalar_one():
                raise RuntimeError(
                    "Legacy watchlist write detected; stop all old app/import processes before migration"
                )
            rows = self.prepare_wallet_rows(json.loads(legacy))
            if rows:
                con.execute(
                    self.wallets.insert(), [{**row, "ordinal": i} for i, row in enumerate(rows)]
                )
            con.execute(self.state.delete().where(self.state.c.key == "wallets"))

    def wallet_snapshot(self):
        # Capture membership and revision under the same writer lock. A concurrent
        # offline append must not label an older in-flight scan as the new revision.
        with self.wallet_transaction() as con:
            rows = [
                json.loads(row[0])
                for row in con.execute(
                    select(self.wallets.c.payload).order_by(self.wallets.c.ordinal)
                )
            ]
            raw = con.execute(
                select(self.state.c.value).where(self.state.c.key == "watch_revision")
            ).scalar()
            return rows, json.loads(raw) if raw is not None else 0

    def wallet_rows(self):
        with self.engine.connect() as con:
            return [
                json.loads(row[0])
                for row in con.execute(
                    select(self.wallets.c.payload).order_by(self.wallets.c.ordinal)
                )
            ]

    def update_watch_state(self, con, bump_revision, updates):
        if bump_revision:
            raw = con.execute(
                select(self.state.c.value).where(self.state.c.key == "watch_revision")
            ).scalar()
            self._put(con, "watch_revision", (json.loads(raw) if raw else 0) + 1)
        for key, value in (updates or {}).items():
            self._put(con, key, value)

    def replace_wallets(self, rows, *, bump_revision=False, state_updates=None):
        prepared = self.prepare_wallet_rows(rows)
        with self.wallet_transaction() as con:
            con.execute(self.wallets.delete())
            if prepared:
                con.execute(
                    self.wallets.insert(), [{**row, "ordinal": i} for i, row in enumerate(prepared)]
                )
            self.update_watch_state(con, bump_revision, state_updates)

    def append_wallets(self, rows, *, state_updates=None):
        prepared = self.prepare_wallet_rows(rows)
        with self.wallet_transaction() as con:
            existing = set(con.execute(select(self.wallets.c.id)).scalars())
            added = [row for row in prepared if row["id"] not in existing]
            if len(existing) + len(added) > 1000:
                raise ValueError("Import would exceed 1,000 watch entries")
            if added:
                offset = con.execute(select(func.max(self.wallets.c.ordinal))).scalar()
                offset = 0 if offset is None else offset + 1
                con.execute(
                    self.wallets.insert(),
                    [{**row, "ordinal": offset + i} for i, row in enumerate(added)],
                )
                self.update_watch_state(con, True, state_updates)
        return {
            "added": len(added),
            "already_present": len(rows) - len(added),
            "watch_entries": len(existing) + len(added),
        }
