from dataclasses import replace
from decimal import Decimal

import pytest

from sniper.engine import chunks, eligible, ingest
from sniper.parser import ParseError, confirmation, deal_status, listed_price, money, parse_offer
from sniper.store import initial_state


def test_exact_button_volume_and_price(offer):
    assert offer.price == "89.87"
    assert offer.volume == "53.088984"
    assert "ПДФ" in offer.terms
    assert offer.id == "O2713926"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e5", "-1", "0", "1.2.3", "1,234.5"])
def test_reject_ambiguous_money(value):
    with pytest.raises(ParseError):
        money(value)


def test_locale_and_no_float():
    assert money("45\u202f000,01") == Decimal("45000.01")
    assert listed_price("ExchangeMMM · 89.87₽ · 6.08K") == Decimal("89.87")
    assert listed_price("‹ Назад") is None


@pytest.mark.parametrize("word", ["МНОГОЗАДАЧНОСТЬ", "многозадасность", "многозадачности"])
def test_warning_is_not_exclusion(card, word):
    offer = parse_offer(card + word)
    assert offer.multitasking
    assert eligible(offer, {"mode": "auto", "auto_max": "90"})


@pytest.mark.parametrize("before,after", [("USDT", "BTC"), ("RUB", "USD"), ("продаёт", "покупает")])
def test_reject_wrong_direction_or_pair(card, before, after):
    with pytest.raises(ParseError):
        parse_offer(card.replace(before, after))


def test_filter_boundaries_bank_and_dedup(offer):
    state = initial_state()
    state["settings"].update(running=True, notify_min="89.87", notify_max="89.87")
    assert ingest(state, offer)
    assert ingest(state, offer) == []
    assert not eligible(replace(offer, payment="Т-Банк"), state["settings"])
    state["settings"]["notify_max"] = "90"
    assert ingest(state, replace(offer, price="89.88"))


def test_notifications_ignore_volume_only_changes_and_include_terms(offer):
    state = initial_state()
    state["settings"].update(running=True, notify_min="80", notify_max="90")
    messages = ingest(state, offer)
    assert len(messages) == 1 and offer.terms in messages[0]["text"]
    assert ingest(state, replace(offer, volume="40")) == []
    assert state["offers"][offer.id]["volume"] == "40"
    assert ingest(state, replace(offer, terms=offer.terms + "\nНовые условия"))


def test_paper_one_active_and_no_real_result(offer):
    state = initial_state()
    state["settings"].update(running=True, mode="paper", auto_max="90")
    messages = ingest(state, offer)
    assert state["active"]["paper"]
    assert "НЕ создавалась" in messages[0]["text"]
    assert ingest(state, replace(offer, id="O777")) == []


def test_confirmation_and_unknown_status():
    text = "Вы уверены, что хотите купить 🟢511.479881 USDT за ₽45 000 RUB?\nЦена за 🟢1 USDT: ₽87.98 RUB"
    assert confirmation(text) == (Decimal("87.98"), Decimal("511.479881"), Decimal("45000"))
    assert deal_status("Вы можете отменить сделку") == "UNKNOWN"
    assert deal_status("Сделка #D123 отменена") == "CANCELLED"
    assert deal_status("Вы отметили оплату") == "PAID"


def test_utf16_message_chunks():
    text = "🙂" * 5000
    parts = list(chunks(text))
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3400 for part in parts)
