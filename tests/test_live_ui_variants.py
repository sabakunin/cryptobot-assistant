from decimal import Decimal

import pytest

from sniper.control import handle_action, menu
from sniper.parser import bot_money, parse_offer
from sniper.store import initial_state
from sniper.telegram_ui import InterfaceChanged, Key, Screen
from sniper.trading import Execution, no_active_deals


@pytest.mark.parametrize(
    "raw,wanted", [("1,099", "1099"), ("1,067.971965", "1067.971965"),
                   ("1,234,567.89", "1234567.89"), ("89,99", "89.99"), ("51 227", "51227")],
)
def test_actual_cryptobot_number_formats(raw, wanted):
    assert bot_money(raw) == Decimal(wanted)


def test_thousands_volume_and_exact_buy_button(card):
    text = card.replace("53 USDT", "1,099 USDT")
    offer = parse_offer(text, ["📈 Купить 1,067.971965 USDT"])
    assert offer.volume == "1067.971965"
    assert offer.amount_source == "кнопка покупки"
    assert parse_offer(text, ["📈 Купить USDT"]).volume == "1099"


@pytest.mark.parametrize("word", ["многозачность", "многозадачка", "многозадасность"])
def test_actual_multitasking_variants(card, word):
    assert parse_offer(card + word).multitasking


class PreparationUI:
    def __init__(self, screens):
        self.screens = iter(screens)
        self.calls = []

    async def click(self, screen, key, **kwargs):
        assert key.data != b"create", "Preparation must never submit a real deal"
        self.calls.append(key.data)
        return next(self.screens)


async def test_live_fixed_amount_skips_maximum_screen(card):
    offer = parse_offer(card, ["📈 Купить 1,067.971965 USDT"])
    opened = Screen(1, card, (Key("📈 Купить 1,067.971965 USDT", b"buy"),))
    confirmed = Screen(
        1, "Вы уверены, что хотите купить 1,067.971965 USDT за 96 000 RUB?\nЦена за 1 USDT: 89.89 RUB",
        (Key("📤 Создать сделку", b"create"), Key("‹ Назад к объявлению", b"back")),
    )
    ui = PreparationUI([confirmed])
    result, price, volume, rubles = await Execution(None, ui, None, None, None).prepare(offer, opened)
    assert result == confirmed
    assert (price, volume, rubles) == (Decimal("89.89"), Decimal("1067.971965"), Decimal("96000"))
    assert ui.calls == [b"buy"]


async def test_live_variable_maximum_and_whole_ruble_rounding(card):
    offer = parse_offer(card, ["📈 Купить USDT"])
    opened = Screen(1, card, (Key("📈 Купить USDT", b"buy"),))
    amount = Screen(1, "Пришлите сумму USDT", (Key("Макс. · 590.728548 USDT", b"max"),))
    confirmed = Screen(
        1, "Вы уверены, что хотите купить 590.728548 USDT за 53 160 RUB?\nЦена за 1 USDT: 89.99 RUB",
        (Key("📤 Создать сделку", b"create"),),
    )
    ui = PreparationUI([amount, confirmed])
    _, _, volume, rubles = await Execution(None, ui, None, None, None).prepare(offer, opened)
    assert volume == Decimal("590.728548") and rubles == Decimal("53160")
    assert ui.calls == [b"buy", b"max"]


async def test_total_mismatch_still_blocks_submission(card):
    offer = parse_offer(card, ["Купить 10 USDT"])
    opened = Screen(1, card, (Key("Купить 10 USDT", b"buy"),))
    confirmed = Screen(1, "Вы уверены, что хотите купить 10 USDT за 901 RUB?\nЦена за 1 USDT: 90 RUB",
                       (Key("Создать сделку", b"create"),))
    ui = PreparationUI([confirmed])
    with pytest.raises(InterfaceChanged, match="Total differs"):
        await Execution(None, ui, None, None, None).prepare(offer, opened)
    assert ui.calls == [b"buy"]


def test_user_menu_has_only_two_real_modes():
    state = initial_state()
    buttons = menu(state)["buttons"]
    modes = {item.get("callback_data") for row in buttons for item in row if item.get("callback_data", "").startswith("mode:")}
    assert modes == {"mode:notify", "mode:auto"}
    handle_action(state, "mode:paper", {})
    assert state["settings"]["mode"] == "notify"


def test_empty_active_deals_requires_all_account_filters():
    text = "Здесь вы можете посмотреть свои сделки за всё время.\nКоличество подходящих сделок: 0"
    labels = ["Активные", "Все типы", "Все монеты", "Все способы оплаты"]
    screen = Screen(1, text, tuple(Key(label, b"filter") for label in labels))
    assert no_active_deals(screen)
    for omitted in labels:
        narrowed = Screen(1, text, tuple(key for key in screen.keys if key.text != omitted))
        assert not no_active_deals(narrowed)
    assert not no_active_deals(Screen(1, text.replace("всё время", "16 ноября"), screen.keys))
    assert not no_active_deals(Screen(1, text.replace(": 0", ": 1"), screen.keys))
