"""Execution state machine. Real network execution is never used in tests or demos."""

import re
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal

from telethon import errors

from .engine import active_buttons, audit, ingest, notice
from .parser import NUMBER, bot_money, confirmation, deal_reference, deal_status, is_trade_chat_notice
from .telegram_ui import InterfaceChanged, navigation_retryable

TERMINAL = {"CANCELLED", "COMPLETED", "REJECTED"}


def no_active_deals(screen):
    explicit = bool(
        re.search(
            r"(?:нет\s+(?:активных|открытых)\s+сделок|(?:активных|открытых)\s+сделок\s+нет|"
            r"(?:активные|открытые)\s+сделки\s*:\s*0\b|у вас пока нет сделок)",
            screen.text,
            re.I,
        )
    )
    labels = {key.text.strip() for key in screen.keys}
    scoped_empty = (
        {"Активные", "Все типы", "Все монеты", "Все способы оплаты"} <= labels
        and "за всё время" in screen.text.casefold()
        and re.search(r"Количество\s+подходящих\s+сделок\s*:\s*0\b", screen.text, re.I)
    )
    return explicit or bool(scoped_empty)


class Execution:
    def __init__(self, store, ui, config, owner, wake):
        self.store, self.ui, self.config, self.owner, self.wake = store, ui, config, owner, wake

    async def permission(self):
        if not self.config.live_trading_enabled:
            raise RuntimeError("Real execution is disabled")
        if not await self.store.owns(self.owner):
            raise RuntimeError("Worker lease was lost")

    async def reconcile(self):
        state = await self.store.read()
        active = state["active"]
        if active and active.get("paper"):
            return
        if active and active.get("managed"):
            if not active.get("deal_id"):
                # Cannot associate an unknown operation with an arbitrary trade. Human binding required.
                await self.store.apply(
                    lambda s: [
                        notice(
                            "⚠️ Исход операции неизвестен. Повторное создание заблокировано. "
                            "Проверь «Мои сделки» в CryptoBot. Для привязки найденной сделки: /trade D123456. "
                            "Если сделки точно нет: /resolve — подтверждение вручную."
                        )
                    ]
                )
                self.wake.set()
                return
            screen = await self.ui.open_deal(active["deal_id"])
            await self.observe(screen)
            return
        screen = await self.ui.active_deals()
        empty = no_active_deals(screen)

        def apply(s):
            s["account_checked"] = empty
            if not empty:
                s["settings"]["running"] = False
            revision = s.pop("reconcile_panel_revision", None)
            arm_after = s.pop("arm_after_reconcile", False)
            if revision is not None:
                if revision != s.get("ui_revision"):
                    return []
                from .control import handle_action, panel_response

                message = notice(
                    "✅ Активных сделок нет."
                    if empty
                    else "Не удалось подтвердить отсутствие сделок. Открой CryptoBot и проверь статус."
                )
                if empty and arm_after and s["settings"]["mode"] == "auto":
                    message = handle_action(s, "run", {"live_enabled": self.config.live_trading_enabled})[0]
                return [panel_response(s, message)]
            return [
                notice(
                    "✅ По экрану CryptoBot активных сделок нет."
                    if empty
                    else "⏸ Не удалось подтвердить отсутствие активных сделок. "
                    "Проверь «Мои сделки». /inspect покажет распознанный экран. "
                    "После ручной проверки отсутствия сделок: /resolve."
                )
            ]

        await self.store.apply(apply)
        self.wake.set()

    async def prepare(self, offer, screen):
        """Navigate to confirmation without submitting it (also used by live diagnostics)."""
        amount_screen = await self.ui.click(
            screen, screen.find(r"^\W*Купить\s+(?:" + NUMBER + r"\s*)?USDT\s*$"), fresh=False
        )
        if any(re.search(r"^\W*Создать\s+сделку\s*$", key.text, re.I) for key in amount_screen.keys):
            # Fixed-size offers skip amount selection. The exact volume comes
            # from the observed Buy button, never the rounded availability text.
            if offer.amount_source != "кнопка покупки":
                raise InterfaceChanged("Fixed purchase has no exact observed amount")
            expected_amount = Decimal(offer.volume)
            confirmed = amount_screen
        else:
            maximum = amount_screen.find(r"^\W*Макс\.?\s*[·:—-]?\s*" + NUMBER + r"\s*USDT\s*$")
            max_value = re.search(r"(" + NUMBER + r")\s*USDT", maximum.text, re.I)
            if max_value is None:
                raise InterfaceChanged("Maximum amount is not readable")
            expected_amount = bot_money(max_value.group(1))
            confirmed = await self.ui.click(amount_screen, maximum, fresh=False)
        price, volume, rubles = confirmation(confirmed.text)
        if volume != expected_amount:
            raise InterfaceChanged("Confirmation volume differs from selected maximum")
        # CryptoBot rounds the final RUB display to whole rubles on some cards.
        # Match the precision actually displayed, rather than assuming kopecks.
        ruble_precision = Decimal(1).scaleb(rubles.as_tuple().exponent)
        if (price * volume).quantize(ruble_precision, rounding=ROUND_HALF_UP) != rubles:
            raise InterfaceChanged("Total differs from price times volume; unknown fees or format")
        confirmed.find(r"^\W*Создать\s+сделку\s*$")
        return confirmed, price, volume, rubles

    async def buy(self, offer, screen):
        await self.permission()
        transaction_id = uuid.uuid4().hex[:12]
        reserved = False

        def reserve(s):
            nonlocal reserved
            if (
                not s["account_checked"]
                or s["settings"]["mode"] != "auto"
                or not s["settings"]["running"]
                or s["active"]
            ):
                return []
            messages = ingest(s, offer)
            if not messages:
                return []
            reserved = True
            s["active"] = {
                "id": transaction_id,
                "offer_id": offer.id,
                "paper": False,
                "managed": True,
                "stage": "PREPARING",
                "link": offer.link,
                "message_id": screen.id,
                "deal_id": None,
                "time": time.time(),
            }
            audit(s, "preparing", offer_id=offer.id)
            return messages + [notice("⏳ Проверяю актуальную цену и максимальную сумму перед созданием.")]

        await self.store.apply(reserve)
        self.wake.set()
        if not reserved:
            return
        attempted = False
        try:
            confirmed, price, volume, rubles = await self.prepare(offer, screen)
            create = confirmed.find(r"^\W*Создать\s+сделку\s*$")
            await self.permission()

            def mark_attempt(s):
                nonlocal attempted
                active = s["active"]
                if not active or active["id"] != transaction_id:
                    raise RuntimeError("Reservation was lost")
                if (
                    not s["settings"]["running"]
                    or s["settings"]["mode"] != "auto"
                    or price > Decimal(s["settings"]["auto_max"])
                ):
                    raise RuntimeError("Stopped or price exceeds current threshold")
                # Persist BEFORE the external side effect. Crash afterwards means UNKNOWN, not a retry.
                active.update(
                    stage="ATTEMPTING",
                    price=str(price),
                    volume=str(volume),
                    rubles=str(rubles),
                    message_id=confirmed.id,
                )
                audit(s, "attempting", offer_id=offer.id)
                attempted = True
                return []

            await self.store.apply(mark_attempt)
            result = await self.ui.click(confirmed, create)
            await self.observe(result, associate=True)
        except Exception as error:
            recoverable = navigation_retryable(error) or isinstance(error, errors.FloodWaitError)

            def failure(s):
                active = s["active"]
                if not active or active["id"] != transaction_id:
                    return []
                if attempted:
                    active["stage"] = "UNKNOWN"
                    text = "⚠️ Исход создания сделки неизвестен. Повторять не буду. Проверь CryptoBot и нажми «Проверить статус»."
                else:
                    s["active"] = None
                    if recoverable:
                        # No Create request was attempted. Re-open and re-validate the
                        # offer on the next scan; a failed preparation is not a purchase.
                        s["notified"].pop(f"auto:{offer.id}", None)
                        audit(s, "preparation_interrupted", offer_id=offer.id)
                        return []
                    s["settings"]["running"] = False
                    text = "⏸ Подготовка остановлена до команды создания: экран, курс или состояние изменились. Проверь /inspect."
                audit(s, "unknown" if attempted else "preparation_failed", offer_id=offer.id)
                return [notice(text, active_buttons(active) if attempted else [])]

            await self.store.apply(failure)
            raise
        finally:
            self.wake.set()

    async def observe(self, screen, associate=False):
        if is_trade_chat_notice(screen.text):
            if associate:
                raise InterfaceChanged("Counterparty chat is not a submission result")
            return
        ref, status = deal_reference(screen.text), deal_status(screen.text)

        def apply(s):
            active = s["active"]
            if not active or not active.get("managed"):
                return []
            if associate:
                # Only the immediate response to our one submitted operation can establish this association.
                active.update(deal_id=ref, message_id=screen.id)
            elif not ref or ref != active.get("deal_id"):
                return []
            if ref is None:
                status_value = "UNKNOWN"
            else:
                status_value = status
            previous = active.get("stage")
            if not associate and status_value == "UNKNOWN":
                return []
            if status_value == previous:
                return []
            active["stage"] = status_value
            audit(s, "deal_status", status=status_value, deal_id=ref)
            if status_value in TERMINAL:
                if active.get("offer_id"):
                    s["ignored"] = list(dict.fromkeys(s["ignored"] + [active["offer_id"]]))[-2000:]
                s["active"] = None
                s["account_checked"] = True
                return [
                    notice(
                        f"Сделка #{ref}: {status_value}. "
                        "Объявление исключено из повторного подбора. Поиск может продолжаться."
                    )
                ]
            labels = {
                "REQUESTED": "Заявка создана; ожидает ответа продавца",
                "ACCEPTED": "Продавец принял сделку",
                "PAID": "Отмечена отправка оплаты",
                "UNKNOWN": "Статус не распознан — повторное создание заблокировано",
            }
            return [
                notice(
                    f"{labels.get(status_value, status_value)}\n"
                    f"Сделка: #{ref or 'ID не получен'}\n"
                    f"Сумма: {active.get('rubles', '—')} RUB / {active.get('volume', '—')} USDT\n"
                    "Поиск приостановлен, пока сделка открыта.",
                    active_buttons(active),
                )
            ]

        await self.store.apply(apply)
        self.wake.set()

    async def cancel(self, active_id):
        await self.permission()
        state = await self.store.read()
        active = state["active"]
        if not active or active["id"] != active_id or not active.get("cancel_requested"):
            return
        if active.get("stage") not in {"REQUESTED", "ACCEPTED"} or not active.get("deal_id"):
            return
        screen = await self.ui.open_deal(active["deal_id"])
        if deal_reference(screen.text) != active["deal_id"]:
            raise InterfaceChanged("Cancel target does not match the active trade")
        status = deal_status(screen.text)
        if status in TERMINAL or status == "PAID":
            await self.observe(screen)
            return
        if status not in {"REQUESTED", "ACCEPTED"}:
            raise InterfaceChanged("Trade state is ambiguous; cancellation not sent")
        key = screen.find(r"^\W*Отменить\s+(?:сделку|заявку)\s*$")

        def mark(s):
            a = s["active"]
            if not a or a["id"] != active_id or not a.get("cancel_requested"):
                raise RuntimeError("Cancel request is stale")
            a.update(stage="CANCELLING", cancel_requested=False)
            audit(s, "cancelling", deal_id=a["deal_id"])
            return []

        await self.store.apply(mark)
        try:
            await self.permission()
            result = await self.ui.click(screen, key)
            if deal_status(result.text) not in TERMINAL:
                if not re.search(r"(?:уверены|подтвердите).*?отмен", result.text, re.I | re.S):
                    raise InterfaceChanged("Cancellation confirmation screen not recognized")
                other = deal_reference(result.text)
                if other and other != active["deal_id"]:
                    raise InterfaceChanged("Cancellation confirmation refers to another trade")
                confirm = result.find(
                    r"^\W*(?:Да[,!]?\s*)?(?:отменить(?:\s+сделку|\s+заявку)?|подтвердить\s+отмену)\s*$"
                )
                await self.permission()
                result = await self.ui.click(result, confirm)
            # A terminal response without an ID is not enough to release the one-trade lock.
            await self.observe(result)
            latest = (await self.store.read())["active"]
            if latest and latest["id"] == active_id and latest["stage"] == "CANCELLING":
                raise InterfaceChanged("Cancellation not confirmed")
        except Exception:

            def unknown(s):
                a = s["active"]
                if a and a["id"] == active_id:
                    a["stage"] = "UNKNOWN"
                    return [
                        notice(
                            "⚠️ Отмена не подтверждена. Поиск остаётся на паузе. Проверь сделку в CryptoBot.",
                            active_buttons(a),
                        )
                    ]
                return []

            await self.store.apply(unknown)
            raise
        finally:
            self.wake.set()
