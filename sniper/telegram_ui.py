"""Real MTProto UI driver. Callback data are always read from live messages, never fabricated."""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field

from telethon import TelegramClient, errors, events, functions
from telethon.sessions import StringSession

from .parser import ParseError, is_trade_chat_notice, listed_price
from .security import safe_link


class InterfaceChanged(RuntimeError):
    pass


def navigation_retryable(error):
    """Only safe before submission; an active/uncertain transaction must stay blocked."""
    return isinstance(
        error, (InterfaceChanged, ParseError, TimeoutError, ConnectionError, errors.BotResponseTimeoutError)
    )


@dataclass(frozen=True)
class Key:
    text: str
    data: bytes | None = None
    url: str | None = None


@dataclass(frozen=True)
class Screen:
    id: int
    text: str
    keys: tuple[Key, ...] = ()
    links: tuple[str, ...] = ()
    fingerprint: str = field(init=False)

    def __post_init__(self):
        digest = hashlib.sha256(repr((self.id, self.text, self.keys)).encode()).hexdigest()
        object.__setattr__(self, "fingerprint", digest)

    @classmethod
    def from_message(cls, message):
        keys = tuple(
            Key(b.text, getattr(b, "data", None), getattr(b, "url", None))
            for row in (message.buttons or [])
            for b in row
        )
        links = tuple(e.url for e in (message.entities or []) if getattr(e, "url", None))
        return cls(message.id, message.raw_text or "", keys, links)

    def find(self, pattern: str) -> Key:
        matches = [k for k in self.keys if k.data is not None and re.search(pattern, k.text, re.I)]
        if len(matches) != 1:
            raise InterfaceChanged("Expected one matching button")
        return matches[0]

    @property
    def offer_link(self):
        return next(
            (safe_link(x) for x in self.links if safe_link(x) != "https://t.me/CryptoBot"),
            "https://t.me/CryptoBot",
        )

    @property
    def is_listing(self):
        return bool(re.search(r"купить\s+USDT\s+за\s+RUB\s+через\s+СБП", self.text, re.I))

    def listings(self):
        if not self.is_listing:
            raise InterfaceChanged("Expected USDT/RUB/SBP offer list")
        return [
            (k, listed_price(k.text))
            for k in self.keys
            if k.data is not None and listed_price(k.text) is not None
        ]

    @property
    def is_first_page(self):
        if not self.is_listing:
            return False
        pages = [k.text.strip() for k in self.keys if re.fullmatch(r"[\s·.•‹›«»]*\d+[\s·.•‹›«»]*", k.text)]
        return not pages or any(re.fullmatch(r"[·.•]+\s*1\s*[·.•]+", text) for text in pages)


class TelegramUI:
    def __init__(self, client, peer):
        self.client, self.peer = client, peer
        self.changed = asyncio.Event()
        self.latest = None
        self.latest_seen = 0.0
        self.screen_revision = 0
        self.action_interval = 0.2
        self.last_action = 0.0
        self.action_lock = asyncio.Lock()

    def remember(self, screen):
        # Deal chat messages are independent notifications, never navigation responses.
        if is_trade_chat_notice(screen.text):
            return False
        if self.latest and screen.id < self.latest.id:
            return False
        self.latest = screen
        self.latest_seen = time.monotonic()
        self.screen_revision += 1
        self.changed.set()
        return True

    @classmethod
    async def connect(cls, config, cipher):
        encrypted = config.user_session_encrypted.get_secret_value()
        if not encrypted:
            encrypted = config.user_session_file.read_text(encoding="utf-8").strip()
        session = cipher.fernet.decrypt(encrypted.encode()).decode()
        client = TelegramClient(
            StringSession(session),
            config.telegram_api_id,
            config.telegram_api_hash.get_secret_value(),
            request_retries=0,
            flood_sleep_threshold=0,
            connection_retries=5,
            retry_delay=2,
            sequential_updates=False,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Session is not authorized")
        peer = await client.get_entity("CryptoBot")
        if not getattr(peer, "bot", False):
            await client.disconnect()
            raise RuntimeError("CryptoBot did not resolve to a bot")
        ui = cls(client, peer)

        async def remember(event):
            ui.remember(Screen.from_message(event.message))

        client.add_event_handler(remember, events.NewMessage(chats=peer, incoming=True))
        client.add_event_handler(remember, events.MessageEdited(chats=peer, incoming=True))
        return ui

    async def current(self, *, force=False):
        if (
            not force
            and self.latest
            and not is_trade_chat_notice(self.latest.text)
            and time.monotonic() - self.latest_seen < 2
        ):
            return self.latest
        requested_revision = self.screen_revision
        messages = await self.client.get_messages(self.peer, limit=20)
        # New messages from sellers can sit below the actual market screen.
        message = next(
            (m for m in messages if not m.out and not is_trade_chat_notice(m.raw_text or "")), None
        )
        if message is None and self.latest:
            # A burst of chat notifications must not bury the known UI message.
            message = await self.client.get_messages(self.peer, ids=self.latest.id)
            if message and (message.out or is_trade_chat_notice(message.raw_text or "")):
                message = None
        if message is None:
            raise InterfaceChanged("No bot message")
        if self.latest and self.screen_revision != requested_revision:
            # An edit received during the history request is newer than its snapshot.
            return self.latest
        self.latest = Screen.from_message(message)
        self.latest_seen = time.monotonic()
        return self.latest

    def ready_listing(self, previous_fingerprint=None):
        if (
            self.latest
            and self.latest.is_first_page
            and self.latest.fingerprint != previous_fingerprint
            and time.monotonic() - self.latest_seen < 2
        ):
            return self.latest
        return None

    async def read_message(self, message_id):
        message = await self.client.get_messages(self.peer, ids=message_id)
        if message is None or message.out:
            raise InterfaceChanged("Message no longer available")
        return Screen.from_message(message)

    async def wait_changed(self, previous, allow_same=False, previous_revision=None):
        # Prefer edits delivered by Telegram; history is only a timeout fallback.
        deadline = time.monotonic() + (0.08 if allow_same else 5)
        while time.monotonic() < deadline:
            self.changed.clear()
            if (
                self.latest
                and not is_trade_chat_notice(self.latest.text)
                and (
                    self.latest.fingerprint != previous.fingerprint
                    or (
                        allow_same
                        and previous_revision is not None
                        and self.screen_revision > previous_revision
                        and self.latest.id == previous.id
                    )
                )
            ):
                return self.latest
            try:
                await asyncio.wait_for(self.changed.wait(), max(0.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        current = await self.current(force=True)
        if current.fingerprint != previous.fingerprint or allow_same:
            return current
        raise InterfaceChanged("Bot did not change the screen")

    async def click(self, screen: Screen, key: Key, *, allow_same=False, fresh=True):
        if key.data is None or key not in screen.keys:
            raise InterfaceChanged("Button is not an observed callback")
        async with self.action_lock:
            await self.pace()
            current = await self.current(force=fresh)
            if current.fingerprint != screen.fingerprint:
                raise InterfaceChanged("Screen changed before click")
            self.latest = screen
            self.changed.clear()
            revision = self.screen_revision
            # NO automatic retries: this method may submit a financial operation.
            answer = await self.client(
                functions.messages.GetBotCallbackAnswerRequest(
                    peer=self.peer, msg_id=screen.id, data=key.data
                )
            )
            if getattr(answer, "alert", False):
                raise InterfaceChanged("CryptoBot returned an alert; inspect the chat")
            return await self.wait_changed(screen, allow_same=allow_same, previous_revision=revision)

    async def pace(self):
        delay = self.last_action + self.action_interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_action = time.monotonic()

    async def market(self):
        async with self.action_lock:
            await self.pace()
            previous = await self.current()
            self.latest = previous
            self.changed.clear()
            await self.client.send_message(self.peer, "/p2p")
            return await self.wait_changed(previous)

    async def offer_list(self, *, reset=False):
        screen = await self.current()
        if screen.is_listing and not reset:
            return screen
        screen = await self.market()
        screen = await self.click(screen, screen.find(r"^\W*Купить\s*$"), fresh=False)
        screen = await self.click(screen, screen.find(r"\bUSDT\b"), fresh=False)
        screen = await self.click(screen, screen.find(r"^\W*СБП(?:\s|·|$)"), fresh=False)
        if not screen.is_listing:
            raise InterfaceChanged("USDT/RUB/SBP list did not open")
        return screen

    async def refresh_first_page(self):
        screen = await self.offer_list()
        pages = [key for key in screen.keys if re.fullmatch(r"[\s·.•‹›«»]*\d+[\s·.•‹›«»]*", key.text)]
        if pages:
            key = screen.find(r"^[\s·.•]*1[\s·.•]*$")
            result = await self.click(screen, key, allow_same=True, fresh=False)
        else:
            # CryptoBot hides pagination when all offers fit on one page.
            # Re-open the observed SBP category to refresh that same first page.
            payments = await self.click(screen, screen.find(r"^\W*Назад\s*$"), fresh=False)
            result = await self.click(payments, payments.find(r"^\W*СБП(?:\s|·|$)"), fresh=False)
        if not result.is_listing:
            raise InterfaceChanged("First page refresh returned another screen")
        return result

    async def my_deals(self):
        screen = await self.market()
        return await self.click(screen, screen.find(r"Мои\s+сделки"))

    async def active_deals(self):
        screen = await self.my_deals()
        labels = {key.text.strip() for key in screen.keys}
        if "Активные" in labels:
            return screen
        if "Все статусы" in labels:
            screen = await self.click(screen, screen.find(r"^Все\s+статусы$"))
            screen = await self.click(screen, screen.find(r"^\W*Активные(?:\s+сделки)?(?:\s*·\s*\d+)?\W*$"))
            return screen
        if any(re.search(r"^\W*Активные(?:\s+сделки)?\W*$", key.text, re.I) for key in screen.keys):
            return await self.click(screen, screen.find(r"^\W*Активные(?:\s+сделки)?\W*$"))
        return screen  # Unrecognized scope will not be accepted as an empty account.

    async def open_deal(self, deal_id):
        screen = await self.current(force=True)
        if re.search(r"#" + re.escape(deal_id) + r"\b", screen.text, re.I):
            return screen
        screen = await self.my_deals()
        if re.search(r"#" + re.escape(deal_id) + r"\b", screen.text, re.I):
            return screen
        return await self.click(screen, screen.find(r"#?" + re.escape(deal_id) + r"\b"))
