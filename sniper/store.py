import time
import uuid
from collections.abc import Callable

from sqlalchemy import BigInteger, Float, Integer, String, Text, case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .security import Cipher


class Base(DeclarativeBase):
    pass


class State(Base):
    __tablename__ = "app_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[str] = mapped_column(Text)


class Inbox(Base):
    __tablename__ = "telegram_inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    done: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created: Mapped[float] = mapped_column(Float)


class Outbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due: Mapped[float] = mapped_column(Float, index=True)
    sent: Mapped[int] = mapped_column(Integer, default=0, index=True)


class Lease(Base):
    __tablename__ = "worker_lease"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(40))
    expires: Mapped[float] = mapped_column(Float)


def initial_state():
    return {
        "settings": {
            "mode": "auto",
            "running": False,
            "notify_min": None,
            "notify_max": None,
            "auto_max": None,
            "min_order_rub": None,
        },
        "pending": None,
        "active": None,
        "offers": {},
        "notified": {},
        "ignored": [],
        "closed_deals": {},
        "reconcile_requested": False,
        "account_checked": False,
        "audit": [],
        "stats": {"observed": 0, "eligible": 0, "filtered": 0, "simulated": 0, "parse_errors": 0},
        "last_seen": None,
    }


def audit(state, action, **details):
    state["audit"].append({"time": int(time.time()), "action": action, **details})
    state["audit"] = state["audit"][-100:]


class Store:
    def __init__(self, url: str, key: str):
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"timeout": 30}
        else:
            options.update(pool_size=2, max_overflow=1)
            options["connect_args"] = {"statement_cache_size": 0}
        self.engine = create_async_engine(url, **options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.cipher = Cipher(key)
        self.insert = sqlite_insert if url.startswith("sqlite") else pg_insert

    async def initialize(self):
        async with self.sessions.begin() as session:
            await session.execute(
                self.insert(State)
                .values(id=1, revision=0, payload=self.cipher.seal(initial_state()))
                .on_conflict_do_nothing(index_elements=[State.id])
            )
        # Fail startup immediately for a wrong encryption key, never reset persisted state.
        await self.read()

    async def claim(self, owner: str, ttl=120):
        async with self.sessions.begin() as session:
            await session.execute(
                self.insert(Lease)
                .values(id=1, owner="", expires=0)
                .on_conflict_do_nothing(index_elements=[Lease.id])
            )
            result = await session.execute(
                update(Lease)
                .where(Lease.id == 1, (Lease.owner == owner) | (Lease.expires < time.time()))
                .values(owner=owner, expires=time.time() + ttl)
                .returning(Lease.owner)
            )
            return result.scalar_one_or_none() == owner

    async def owns(self, owner):
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    select(Lease.id).where(Lease.id == 1, Lease.owner == owner, Lease.expires > time.time())
                )
            )

    async def release(self, owner):
        async with self.sessions.begin() as session:
            await session.execute(update(Lease).where(Lease.owner == owner).values(expires=0))

    async def read(self):
        async with self.sessions() as session:
            payload = await session.scalar(select(State.payload).where(State.id == 1))
            return self.cipher.open(payload)

    async def apply(self, change: Callable, inbox_id: int | None = None):
        async with self.sessions.begin() as session:
            # Atomic UPDATE acquires a row write lock on both PostgreSQL and SQLite.
            payload = (
                await session.execute(
                    update(State)
                    .where(State.id == 1)
                    .values(revision=State.revision + 1)
                    .returning(State.payload)
                )
            ).scalar_one()
            if inbox_id is not None:
                done = await session.scalar(select(Inbox.done).where(Inbox.id == inbox_id))
                if done != 0:
                    return False
            state = self.cipher.open(payload)
            messages = change(state) or []
            await session.execute(update(State).where(State.id == 1).values(payload=self.cipher.seal(state)))
            for message in messages:
                if message.get("kind") == "panel":
                    values = dict(
                        id=f"panel:{message['_recipient']}" if message.get("_recipient") else "control-panel",
                        payload=self.cipher.seal({**message, "_delivery": uuid.uuid4().hex}),
                        attempts=0,
                        due=time.time(),
                        sent=0,
                    )
                    await session.execute(
                        self.insert(Outbox)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=[Outbox.id],
                            set_={key: value for key, value in values.items() if key != "id"},
                        )
                    )
                    continue
                session.add(
                    Outbox(
                        id=uuid.uuid4().hex,
                        payload=self.cipher.seal(message),
                        attempts=0,
                        due=time.time(),
                        sent=0,
                    )
                )
            if inbox_id is not None:
                await session.execute(update(Inbox).where(Inbox.id == inbox_id).values(done=1))
            return True

    async def enqueue(self, data: dict):
        async with self.sessions.begin() as session:
            await session.execute(
                self.insert(Inbox)
                .values(id=data["update_id"], payload=self.cipher.seal(data), done=0, created=time.time())
                .on_conflict_do_nothing(index_elements=[Inbox.id])
            )

    async def inbox(self):
        async with self.sessions() as session:
            rows = (
                await session.scalars(select(Inbox).where(Inbox.done == 0).order_by(Inbox.id).limit(50))
            ).all()
            return [(r.id, self.cipher.open(r.payload)) for r in rows]

    async def outgoing(self):
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Outbox)
                    .where(Outbox.sent == 0, Outbox.due <= time.time())
                    .order_by(
                        case(((Outbox.id == "control-panel") | Outbox.id.like("panel:%"), 0), else_=1),
                        Outbox.due,
                    )
                    .limit(50)
                )
            ).all()
            return [(r.id, r.attempts, self.cipher.open(r.payload)) for r in rows]

    async def next_due(self):
        from sqlalchemy import func

        async with self.sessions() as session:
            return await session.scalar(select(func.min(Outbox.due)).where(Outbox.sent == 0))

    async def sent(self, message_id: str, expected_delivery=None):
        async with self.sessions.begin() as session:
            query = update(Outbox).where(Outbox.id == message_id)
            if expected_delivery:
                current = await session.scalar(select(Outbox.payload).where(Outbox.id == message_id))
                if not current or self.cipher.open(current).get("_delivery") != expected_delivery:
                    return False
                query = query.where(Outbox.payload == current)
            result = await session.execute(query.values(sent=1))
            return bool(result.rowcount)

    async def retry(self, message_id: str, attempts: int, delay: float, expected_delivery=None):
        async with self.sessions.begin() as session:
            query = update(Outbox).where(Outbox.id == message_id)
            if expected_delivery:
                current = await session.scalar(select(Outbox.payload).where(Outbox.id == message_id))
                if not current or self.cipher.open(current).get("_delivery") != expected_delivery:
                    return
                query = query.where(Outbox.payload == current)
            await session.execute(query.values(attempts=attempts + 1, due=time.time() + max(1, delay)))

    async def clean(self):
        from sqlalchemy import delete

        cutoff = time.time() - 7 * 86400
        async with self.sessions.begin() as session:
            await session.execute(delete(Inbox).where(Inbox.done == 1, Inbox.created < cutoff))
            await session.execute(delete(Outbox).where(Outbox.sent == 1, Outbox.due < cutoff))
