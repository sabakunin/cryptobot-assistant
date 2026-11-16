import time
import uuid
from decimal import Decimal

from .parser import Offer
from .store import audit

CAPABILITY_TEXT = (
    "Источник — первая страница USDT / RUB / СБП в CryptoBot. "
    "Уведомления отправляют курс и условия; автопокупка выбирает максимальную сумму "
    "и создаёт сделку по заданному порогу. Для автопокупки нужны MONITOR_SOURCE=userbot, "
    "LIVE_TRADING_ENABLED=true и отдельное включение кнопкой владельца. "
    "При активной или неопределённой сделке новые покупки приостанавливаются. "
    "Оплату владелец выполняет самостоятельно."
)


def notice(text, buttons=None):
    return {"text": text, "buttons": buttons or []}


def button(text, callback=None, url=None):
    return {"text": text, **({"callback_data": callback} if callback else {"url": url})}


def chunks(text, limit=3400):
    # Telegram counts UTF-16 code units for message length.
    current, size = [], 0
    for character in text:
        units = 2 if ord(character) > 0xFFFF else 1
        if size + units > limit:
            yield "".join(current)
            current, size = [], 0
        current.append(character)
        size += units
    if current:
        yield "".join(current)


def eligible(offer: Offer, settings):
    if offer.payment.casefold().strip() != "сбп":
        return False
    price = Decimal(offer.price)
    if settings["mode"] in {"paper", "auto"}:
        return settings["auto_max"] is not None and price <= Decimal(settings["auto_max"])
    low, high = settings["notify_min"], settings["notify_max"]
    return low is not None and high is not None and Decimal(low) <= price <= Decimal(high)


def configured(settings):
    if settings["mode"] in {"paper", "auto"}:
        return settings["auto_max"] is not None
    return settings["notify_min"] is not None and settings["notify_max"] is not None


def ingest(state, offer: Offer):
    now = time.time()
    state["stats"]["observed"] += 1
    state["last_seen"] = now
    state["offers"].pop(offer.id, None)
    state["offers"][offer.id] = {**offer.data(), "received_at": now}
    while len(state["offers"]) > 100:
        state["offers"].pop(next(iter(state["offers"])))
    settings = state["settings"]
    if not settings["running"] or state["active"] or offer.id in state["ignored"]:
        return []
    if not eligible(offer, settings):
        state["stats"]["filtered"] += 1
        return []
    key = f"{settings['mode']}:{offer.id}"
    fingerprint = offer.notification_fingerprint if settings["mode"] == "notify" else offer.fingerprint
    if state["notified"].get(key) == fingerprint:
        return []
    state["notified"].pop(key, None)
    state["notified"][key] = fingerprint
    while len(state["notified"]) > 2000:
        state["notified"].pop(next(iter(state["notified"])))
    state["stats"]["eligible"] += 1
    paper = settings["mode"] == "paper"
    audit(state, "paper_match" if paper else "eligible", offer_id=offer.id)
    heading = "🧪 СИМУЛЯЦИЯ — реальная сделка НЕ создавалась" if paper else "🎯 Подходящая карточка"
    source = "сообщение CryptoBot" if offer.source == "telegram" else "присланный тобой текст"
    estimate = (Decimal(offer.volume) * Decimal(offer.price)).quantize(Decimal("0.01"))
    description = (
        f"{heading}\n\nОбъявление: #{offer.id}\nПродавец: {offer.seller[:200]}\n"
        f"USDT / RUB · СБП\nКурс: {offer.price} ₽/USDT\nОбъём: {offer.volume} USDT\n"
        f"Цена × объём: ≈ {estimate} ₽ (без неизвестных комиссий)\n"
        f"Источник объёма: {offer.amount_source}\nИсточник карточки: {source}\n"
        "Актуальность и доступность сделки не перепроверены."
    )
    if offer.multitasking:
        description += "\n\n⚠️ В условиях обнаружена «многозадачность» или похожее написание."
    if not offer.terms_complete:
        description += "\n⚠️ Условия отсутствуют или выглядят обрезанными. Открой полную карточку."
    rows = [[button("Открыть CryptoBot", url=offer.link)]]
    if paper:
        state["stats"]["simulated"] += 1
        state["active"] = {
            "id": uuid.uuid4().hex[:12],
            "offer_id": offer.id,
            "paper": True,
            "link": offer.link,
            "time": now,
        }
        description += "\n\nУсловная сделка активна. Обработка новых совпадений приостановлена."
        rows += [[button("Завершить симуляцию", callback=f"close:{state['active']['id']}")]]
    elif settings["mode"] == "notify":
        rows += [[button("Я открыл эту сделку — пауза", callback=f"active:{offer.id}")]]
    if offer.terms:
        description += "\n\n📋 Условия продавца:\n" + offer.terms
    parts = list(chunks(description))
    return [notice(part, rows if index == len(parts) - 1 else []) for index, part in enumerate(parts)]


def observe_deal(state, deal_id):
    """Conservative pause. A message containing #D is not evidence of acceptance/completion."""
    if state["active"] and state["active"].get("deal_id") == deal_id:
        return []
    if state["active"] and not state["active"].get("paper"):
        return []
    state["active"] = {
        "id": uuid.uuid4().hex[:12],
        "offer_id": None,
        "deal_id": deal_id,
        "paper": False,
        "link": "https://t.me/CryptoBot",
        "time": time.time(),
    }
    audit(state, "deal_message_pause", deal_id=deal_id)
    return [
        notice(
            f"⏸ В CryptoBot получено сообщение о сделке #{deal_id}.\n"
            "Поиск совпадений приостановлен. Статус сделки автоматически не определён.\n"
            "Проверь её в CryptoBot; возобновляй поиск только после фактического закрытия.",
            active_buttons(state["active"]),
        )
    ]


def active_buttons(active):
    if active["paper"]:
        return [[button("Завершить симуляцию", callback=f"close:{active['id']}")]]
    rows = [
        [button("Открыть CryptoBot ↗", url=active["link"])],
    ]
    if active.get("managed"):
        if (
            active.get("deal_id")
            and active.get("stage") in {"REQUESTED", "ACCEPTED"}
            and not active.get("cancel_requested")
        ):
            rows.insert(0, [button("❌ Отменить сделку", callback=f"cancel:{active['id']}")])
        rows.append([button("Проверить статус", callback="reconcile")])
    else:
        rows.append([button("Сделка уже закрыта", callback=f"close:{active['id']}")])
    return rows
