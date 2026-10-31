import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from openpyxl import load_workbook
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select
from .watchlist import WatchlistMixin

GROUPS = ("Spot Profit", "Spot Loss", "Futures Profit", "Futures Loss")
HEADERS = ("address", "chain", "coin", "label", "rank", "asset", "decimals", "platform")
EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class BudgetExceeded(ValueError):
    def __init__(self, source, period):
        super().__init__(source + " free budget exhausted")
        self.period = period


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_excel(data):
    if len(data) > 5_000_000:
        raise ValueError("Excel exceeds 5 MB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 50_000_000:
            raise ValueError("Excel expanded data exceeds 50 MB")
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    result, seen = [], set()
    try:
        if set(book.sheetnames) != set(GROUPS):
            raise ValueError("Use exactly these sheets: " + ", ".join(GROUPS))
        for group in GROUPS:
            rows = book[group].iter_rows(values_only=True)
            header = tuple(str(v or "").strip().lower() for v in next(rows, ()))
            if header != HEADERS:
                raise ValueError(f"{group}: headers must be {HEADERS}")
            for line, values in enumerate(rows, 2):
                if not any(v is not None for v in values):
                    continue
                if len(result) >= 1000 or line > 1002:
                    raise ValueError("Maximum 1,000 watch entries per workbook")
                row = dict(
                    zip(
                        HEADERS,
                        (str(v if v is not None else "").strip() for v in values),
                        strict=True,
                    )
                )
                if any(v.startswith("=") for v in row.values()):
                    raise ValueError(f"{group}:{line}: formulas are not accepted")
                chain = row["chain"].lower()
                row["chain"] = chain
                pattern = (
                    SOL
                    if chain == "solana"
                    else EVM
                    if chain in ("ethereum", "arbitrum", "base", "polygon", "bsc")
                    else None
                )
                if pattern is None or not pattern.fullmatch(row["address"]):
                    raise ValueError(f"{group}:{line}: unsupported chain or invalid address")
                if pattern is EVM:
                    row["address"] = row["address"].lower()
                    row["asset"] = row["asset"].lower()
                if not row["coin"]:
                    raise ValueError(f"{group}:{line}: coin required")
                row["rank"] = int(row["rank"] or line - 1)
                if row["rank"] < 1:
                    raise ValueError("rank must be positive")
                row["label"] = row["label"] or f"{group} Trader #{row['rank']}"
                row["asset"] = row["asset"] or "native"
                native_symbols = {
                    "ethereum": "ETH",
                    "solana": "SOL",
                    "arbitrum": "ETH",
                    "base": "ETH",
                    "polygon": "POL",
                    "bsc": "BNB",
                }
                if (
                    group.startswith("Spot")
                    and row["asset"] == "native"
                    and row["coin"].upper() != native_symbols[chain]
                ):
                    raise ValueError(
                        "Native coin symbol does not match chain; provide contract/mint for tokens"
                    )
                if row["asset"] != "native" and not pattern.fullmatch(row["asset"]):
                    raise ValueError(f"{group}:{line}: invalid token contract/mint")
                row["decimals"] = int(
                    row["decimals"] if row["decimals"] else (9 if chain == "solana" else 18)
                )
                if not 0 <= row["decimals"] <= 36:
                    raise ValueError("decimals must be 0..36")
                if row["asset"] != "native" and not dict(zip(HEADERS, values, strict=True)).get(
                    "decimals"
                ):
                    # Zero decimals is a valid explicit entry.
                    if dict(zip(HEADERS, values, strict=True)).get("decimals") != 0:
                        raise ValueError("Token decimals must be explicitly supplied")
                row["group"] = group
                identity = "|".join(
                    row[k] for k in ("group", "chain", "address", "asset", "platform")
                )
                row["id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
                if row["id"] in seen:
                    raise ValueError(f"{group}:{line}: duplicate watch entry")
                seen.add(row["id"])
                result.append(row)
    finally:
        book.close()
    return result


class Store(WatchlistMixin):
    def __init__(self, url):
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, pool_pre_ping=True)
        meta = MetaData()
        self.define_wallet_table(meta)
        self.state = Table(
            "whale_state", meta, Column("key", String(100), primary_key=True), Column("value", Text)
        )
        self.events = Table(
            "whale_events", meta, Column("id", Integer, primary_key=True), Column("payload", Text)
        )
        self.inbox = Table(
            "whale_source_events",
            meta,
            Column("key", String(200), primary_key=True),
            Column("payload", Text),
        )
        self.cycles = Table(
            "whale_report_cycles",
            meta,
            Column("id", String(40), primary_key=True),
            Column("at", String(40)),
            Column("payload", Text),
        )
        self.screens = Table(
            "whale_screen_reports",
            meta,
            Column("id", String(64), primary_key=True),
            Column("kind", String(8)),
            Column("revision", Integer),
            Column("automatic", Integer),
            Column("at", String(40)),
            Column("payload", Text),
        )
        self.deliveries = Table(
            "whale_report_deliveries",
            meta,
            Column("id", Integer, primary_key=True),
            Column("done", Integer),
            Column("payload", Text),
        )
        self.order_events = Table(
            "whale_order_events",
            meta,
            Column("id", Integer, primary_key=True),
            Column("event_key", String(64), unique=True, nullable=False),
            Column("venue", String(32), index=True),
            Column("account", String(100), index=True),
            Column("coin", String(32), index=True),
            Column("payload", Text, nullable=False),
        )
        meta.create_all(self.engine)
        self.migrate_wallets()

    def get(self, key, default=None):
        if key == "wallets":
            return self.wallet_rows()
        with self.engine.connect() as con:
            value = con.execute(select(self.state.c.value).where(self.state.c.key == key)).scalar()
        return json.loads(value) if value is not None else default

    def put(self, key, value):
        if key == "wallets":
            return self.replace_wallets(value)
        with self.engine.begin() as con:
            self._put(con, key, value)

    def _put(self, con, key, value):
        if key == "wallets":
            raise ValueError("Use the transactional watchlist repository")
        encoded = json.dumps(value)
        updated = con.execute(
            self.state.update().where(self.state.c.key == key).values(value=encoded)
        )
        if updated.rowcount == 0:
            con.execute(self.state.insert().values(key=key, value=encoded))

    def save_order_study(self, snapshot_key, snapshot, identity, events, observed_at):
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        venue, account, coin = identity.split(":", 2)
        records = [
            {
                "event_key": hashlib.sha256(
                    (identity + ":" + event["identity"]).encode()
                ).hexdigest(),
                "venue": venue,
                "account": account,
                "coin": coin,
                "payload": json.dumps(
                    {
                        "kind": event["kind"],
                        "venue": venue,
                        "account": account,
                        "coin": coin,
                        "observed_at": observed_at,
                        "evidence": event["evidence"],
                    },
                    allow_nan=False,
                ),
            }
            for event in events
        ]
        with self.engine.begin() as con:
            if snapshot_key:
                self._put(con, snapshot_key, snapshot)
            if records:
                con.execute(
                    insert(self.order_events).on_conflict_do_nothing(index_elements=["event_key"]),
                    records,
                )

    def order_history(self, coin="", account="", limit=100, before_id=0):
        query = select(self.order_events.c.id, self.order_events.c.payload)
        if before_id > 0:
            query = query.where(self.order_events.c.id < before_id)
        if coin:
            query = query.where(self.order_events.c.coin == coin)
        if account:
            query = query.where(self.order_events.c.account == account)
        with self.engine.connect() as con:
            return [
                {"storage_id": row[0], **json.loads(row[1])}
                for row in con.execute(
                    query.order_by(self.order_events.c.id.desc()).limit(max(1, min(200, limit)))
                )
            ]

    @staticmethod
    def holding_event(row, amount, source, previous, at):
        event = None
        if previous is not None:
            with localcontext() as context:
                context.prec = 100
                delta = amount - Decimal(previous["amount"])
            if delta:
                event = {
                    "label": row["label"],
                    "watch_id": row["id"],
                    "coin": row["coin"],
                    "group": row["group"],
                    "chain": row["chain"],
                    "address": row["address"],
                    "kind": "holding_increase" if delta > 0 else "holding_decrease",
                    "delta": str(delta),
                    "before": previous["amount"],
                    "after": str(amount),
                    "from": previous["at"],
                    "at": at,
                    "source": source,
                    "price_usd": None,
                    "value_usd": None,
                    "classification": "unverified: transaction evidence required",
                }
        return event

    def save_scan_observations(self, updates):
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        records, events, seen = [], [], set()
        for update in updates:
            row, amount = update["row"], update["amount"]
            if not amount.is_finite():
                raise ValueError("Non-finite wallet/position quantity")
            future = update["future"]
            key = ("position:" if future else "snapshot:") + row["id"]
            if key in seen:
                raise ValueError("Duplicate snapshot identity in batch")
            seen.add(key)
            at, source = update["at"], update["result"]["source"]
            if future:
                payload = {k: v for k, v in update["result"].items() if k != "orders"}
                payload["at"] = at
            else:
                payload = {"amount": str(amount), "at": at, "source": source}
                event = self.holding_event(row, amount, source, update["previous"], at)
                if event:
                    events.append(event)
            records.append({"key": key, "value": json.dumps(payload, allow_nan=False)})
        if records:
            with self.engine.begin() as con:
                statement = insert(self.state)
                con.execute(
                    statement.on_conflict_do_update(
                        index_elements=["key"], set_={"value": statement.excluded.value}
                    ),
                    records,
                )
                if events:
                    con.execute(
                        self.events.insert(), [{"payload": json.dumps(event)} for event in events]
                    )
        return events

    def observe(self, row, amount, source):
        previous = self.get("snapshot:" + row["id"])
        events = self.save_scan_observations(
            [
                {
                    "row": row,
                    "amount": amount,
                    "future": False,
                    "previous": previous,
                    "at": now(),
                    "result": {"source": source},
                }
            ]
        )
        return events[0] if events else None

    def reports(self, limit=100):
        with self.engine.connect() as con:
            rows = con.execute(
                select(self.events.c.payload).order_by(self.events.c.id.desc()).limit(limit)
            )
            return [json.loads(r[0]) for r in rows]

    def ingest(self, key, payload):
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as con:
                con.execute(self.inbox.insert().values(key=key, payload=json.dumps(payload)))
            return True
        except IntegrityError:
            return False

    def evidence(self, limit=50):
        with self.engine.connect() as con:
            return [
                json.loads(r[0]) for r in con.execute(select(self.inbox.c.payload).limit(limit))
            ]

    def evidence_window(self, start, end):
        # Both SQLite and PostgreSQL can filter the canonical JSON receipt timestamp.
        from sqlalchemy import func

        timestamp = (
            func.json_extract(self.inbox.c.payload, "$.received_at")
            if self.engine.dialect.name == "sqlite"
            else self.inbox.c.payload.cast(__import__("sqlalchemy").JSON)["received_at"].as_string()
        )
        with self.engine.connect() as con:
            rows = con.execute(
                select(self.inbox.c.payload).where(timestamp > start, timestamp <= end)
            )
            return [json.loads(r[0]) for r in rows]

    def save_cycle(self, report, messages=()):
        with self.engine.begin() as con:
            con.execute(
                self.cycles.insert().values(
                    id=report["id"], at=report["completed_at"], payload=json.dumps(report)
                )
            )
            self._put(con, "latest_cycle", report)
            for message in messages:
                con.execute(
                    self.deliveries.insert().values(
                        done=0, payload=json.dumps({"cycle_id": report["id"], "text": message})
                    )
                )

    def enqueue_report(self, source, message):
        """Reuse the durable Telegram outbox; no HTTP calls from feed callbacks."""
        with self.engine.begin() as con:
            con.execute(
                self.deliveries.insert().values(
                    done=0, payload=json.dumps({"cycle_id": source, "text": message})
                )
            )

    def pending_deliveries(self):
        with self.engine.connect() as con:
            return [
                {"id": r[0], **json.loads(r[1])}
                for r in con.execute(
                    select(self.deliveries.c.id, self.deliveries.c.payload)
                    .where(self.deliveries.c.done == 0)
                    .order_by(self.deliveries.c.id)
                    .limit(20)
                )
            ]

    def delivered(self, delivery_id):
        with self.engine.begin() as con:
            con.execute(
                self.deliveries.update().where(self.deliveries.c.id == delivery_id).values(done=1)
            )

    def report_history(self, limit=12):
        with self.engine.connect() as con:
            rows = con.execute(
                select(self.cycles.c.payload).order_by(self.cycles.c.at.desc()).limit(limit)
            )
            return [json.loads(r[0]) for r in rows]

    def save_screen(self, report, automatic=False, cursor_key=None, cursor=None, message=None):
        with self.engine.begin() as con:
            exists = con.execute(
                select(self.screens.c.id).where(self.screens.c.id == report["id"])
            ).scalar()
            if exists:
                if automatic:
                    con.execute(
                        self.screens.update()
                        .where(self.screens.c.id == report["id"])
                        .values(automatic=1)
                    )
            else:
                con.execute(
                    self.screens.insert().values(
                        id=report["id"],
                        kind=report["kind"],
                        revision=report["revision"],
                        automatic=int(automatic),
                        at=report["completed_at"],
                        payload=json.dumps(report),
                    )
                )
            if cursor_key:
                self._put(con, cursor_key, cursor)
            if message and not exists:
                con.execute(
                    self.deliveries.insert().values(
                        done=0, payload=json.dumps({"cycle_id": report["id"], "text": message})
                    )
                )

    def screen_history(self, kind, revision, limit=4, automatic_only=True):
        query = select(self.screens.c.payload).where(
            self.screens.c.kind == kind, self.screens.c.revision == revision
        )
        if automatic_only:
            query = query.where(self.screens.c.automatic == 1)
        with self.engine.connect() as con:
            return [
                json.loads(r[0])
                for r in con.execute(query.order_by(self.screens.c.at.desc()).limit(limit))
            ]

    def screen_by_id(self, report_id):
        with self.engine.connect() as con:
            value = con.execute(
                select(self.screens.c.payload).where(self.screens.c.id == report_id)
            ).scalar()
            return json.loads(value) if value else None

    def cycles_after(self, after, limit=6, revision=None):
        with self.engine.connect() as con:
            query = select(self.cycles.c.payload).where(self.cycles.c.at > after)
            if revision is not None:
                from sqlalchemy import func, JSON

                if self.engine.dialect.name == "sqlite":
                    schema = func.json_extract(self.cycles.c.payload, "$.screen_schema")
                    rev = func.json_extract(self.cycles.c.payload, "$.watch_revision")
                else:
                    payload = self.cycles.c.payload.cast(JSON)
                    schema = payload["screen_schema"].as_integer()
                    rev = payload["watch_revision"].as_integer()
                query = query.where(schema == 1, rev == revision)
            query = query.order_by(self.cycles.c.at).limit(limit)
            return [json.loads(r[0]) for r in con.execute(query)]

    def reserve_budget(self, source, period, cost, limit):
        key = "budget:" + source + ":" + period
        used = self.get(key, 0)
        if used + cost > limit:
            raise BudgetExceeded(source, period)
        self.put(key, used + cost)
        return used + cost

    def reserve_budgets(self, budgets):
        """Check and debit all request caps atomically before outbound I/O."""
        with self.engine.begin() as con:
            entries = []
            for source, period, cost, limit in budgets:
                key = "budget:" + source + ":" + period
                value = con.execute(
                    select(self.state.c.value).where(self.state.c.key == key)
                ).scalar()
                used = json.loads(value) if value is not None else 0
                if used + cost > limit:
                    raise BudgetExceeded(source, period)
                entries.append((key, used + cost))
            for key, value in entries:
                self._put(con, key, value)
