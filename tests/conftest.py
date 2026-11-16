import pytest
from cryptography.fernet import Fernet

from sniper.parser import parse_offer
from sniper.store import Base, Store

CARD = """Объявление #O2713926
ExchangeMMM продаёт USDT за RUB.
583 сделки · 87.99%
Цена за 🟢1 USDT: ₽89.87 RUB · $1.08
Доступный объём: 🟢53 USDT
Сумма сделки: ₽4 771.11 RUB или 🟢53 USDT
Способ оплаты: СБП
Срок оплаты: 15 мин
Условия сделки:
СТРОГО ОДНИМ ПЛАТЕЖОМ
ПДФ ЧЕК ОБЯЗАТЕЛЬНО
"""


@pytest.fixture
def card():
    return CARD


@pytest.fixture
def offer(card):
    return parse_offer(card, ["📈 Купить 53.088984 USDT"], source="telegram")


@pytest.fixture
async def store(tmp_path):
    instance = Store(
        "sqlite+aiosqlite:///" + (tmp_path / "test.db").as_posix(), Fernet.generate_key().decode()
    )
    async with instance.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await instance.initialize()
    yield instance
    await instance.engine.dispose()
