import hashlib
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from .security import safe_link

NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9][0-9 \t\u00a0\u202f]*(?:[.,][0-9]+)?)"
OFFER_ID = re.compile(r"(?:объявление\s*)?#\s*([oо]\d{3,})", re.I)
DEAL_ID = re.compile(r"#\s*([dд]\d{3,})", re.I)


class ParseError(ValueError):
    pass


def money(raw: str) -> Decimal:
    cleaned = re.sub(r"[\s\u00a0\u202f]", "", raw).replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,12})?", cleaned):
        raise ParseError("Ожидается положительное число, например 89.87")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError("Некорректное число") from exc
    if result <= 0 or result > Decimal("100000000000"):
        raise ParseError("Число должно быть больше нуля и не превышать 100000000000")
    return result


def bot_money(raw: str) -> Decimal:
    # CryptoBot groups thousands with commas, including in Russian UI:
    # "1,067.971965 USDT". User-entered rates still use money() separately.
    raw = raw.strip()
    if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?", raw):
        raw = raw.replace(",", "")
    return money(raw)


@dataclass(frozen=True)
class Offer:
    id: str
    price: str
    volume: str
    amount_source: str
    payment: str
    terms: str
    terms_complete: bool
    multitasking: bool
    seller: str
    link: str
    source: str

    def data(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return hashlib.sha256(repr((self.price, self.volume, self.terms, self.payment)).encode()).hexdigest()

    @property
    def notification_fingerprint(self):
        return hashlib.sha256(repr((self.price, self.terms, self.payment)).encode()).hexdigest()


def parse_offer(
    text: str, buttons: list[str] | None = None, link: str | None = None, source: str = "manual"
) -> Offer:
    text = text.replace("\r", "")
    if len(text) > 32000:
        raise ParseError("Карточка слишком длинная")
    match = OFFER_ID.search(text)
    if not match:
        raise ParseError("Нет ID объявления #O…; нужен полный текст карточки")
    if not re.search(r"прода[её]т\s+USDT\s+за\s+RUB", text, re.I):
        raise ParseError("Поддерживается покупка USDT за RUB у продавца")
    price = re.search(r"Цена\s+за[^\n]*?1\s*USDT\s*:[^\d\n]*(" + NUMBER + r")\s*RUB", text, re.I)
    volume = re.search(r"Доступный\s+объ[её]м\s*:[^\d\n]*(" + NUMBER + r")\s*USDT", text, re.I)
    payment = re.search(r"Способ\s+оплаты\s*:\s*([^\n]+)", text, re.I)
    if not price or not volume or not payment:
        raise ParseError("В карточке не найдены цена, доступный объём или способ оплаты")
    amount = bot_money(volume.group(1))
    amount_source = "объём в тексте (может быть округлён)"
    for label in buttons or []:
        buy = re.search(r"Купить\s+(" + NUMBER + r")\s*USDT", label, re.I)
        if buy:
            amount = bot_money(buy.group(1))
            amount_source = "кнопка покупки"
            break
    parts = re.split(r"Условия\s+сделки\s*:\s*", text, maxsplit=1, flags=re.I)
    terms = parts[1].strip() if len(parts) > 1 else ""
    complete = bool(terms) and not terms.endswith(("…", "..."))
    normalized = re.sub(r"[\W_]", "", terms.casefold())
    warning = bool(re.search(r"многоза(?:да)?(?:ч|с)(?:ност|к)", normalized))
    seller = re.search(r"([^\n]+?)\s+прода[её]т\s+USDT", text, re.I)
    return Offer(
        id="O" + match.group(1)[1:],
        price=str(bot_money(price.group(1))),
        volume=str(amount),
        amount_source=amount_source,
        payment=payment.group(1).strip(),
        terms=terms,
        terms_complete=complete,
        multitasking=warning,
        seller=seller.group(1).strip() if seller else "Не указан",
        link=safe_link(link),
        source=source,
    )


def is_trade_chat_notice(text: str) -> bool:
    """CryptoBot's counterparty-message envelope, not the quoted seller's status claims."""
    return bool(
        re.match(
            r"^\W*(?:Новое\s+)?Сообщение\s+от\s+[^\r\n]+?\s+по\s+сделке\s*#[DД]\d+\b",
            text,
            re.I,
        )
    )


def deal_reference(text: str) -> str | None:
    match = DEAL_ID.search(text)
    return "D" + match.group(1)[1:] if match else None


def listed_price(label: str) -> Decimal | None:
    found = re.search(r"(" + NUMBER + r")\s*(?:₽|RUB|Р)(?=\s|·|$)", label, re.I)
    return bot_money(found.group(1)) if found else None


def listed_amount_ceiling(label: str) -> Decimal | None:
    """Conservative upper bound of the rounded RUB range, never an exact total."""
    tail = re.search(r"(?:₽|RUB|Р)\s*·\s*(.+)$", label, re.I)
    if not tail:
        return None
    parts = re.split(r"\s*[–—-]\s*", tail.group(1).strip())
    if len(parts) not in {1, 2}:
        return None
    ceilings = []
    for part in parts:
        match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*([KКMМ]?)", part, re.I)
        if not match:
            return None
        value = Decimal(match.group(1).replace(",", "."))
        scale = {"K": 1000, "К": 1000, "M": 1000000, "М": 1000000, "": 1}[match.group(2).upper()]
        # Allow a whole display unit for rounding/truncation; exact confirmation
        # remains authoritative. Unknown formats are never excluded here.
        ceilings.append((value + Decimal(1).scaleb(value.as_tuple().exponent)) * scale)
    return max(ceilings)


def confirmation(text: str) -> tuple[Decimal, Decimal, Decimal]:
    amount = re.search(
        r"купить[^\d\n]*(" + NUMBER + r")\s*USDT\s*за[^\d\n]*(" + NUMBER + r")\s*RUB", text, re.I
    )
    price = re.search(r"Цена\s+за[^\n]*?1\s*USDT\s*:[^\d\n]*(" + NUMBER + r")\s*RUB", text, re.I)
    if not amount or not price:
        raise ParseError("Не распознан экран подтверждения покупки USDT/RUB")
    return bot_money(price.group(1)), bot_money(amount.group(1)), bot_money(amount.group(2))


def deal_status(text: str) -> str:
    """Conservative Russian UI recognition; ambiguous text never means success."""
    if is_trade_chat_notice(text):
        return "UNKNOWN"
    # Seller conditions and quoted messages cannot establish a financial status.
    text = re.split(r"Условия\s+(?:сделки|продавца)\s*:|Сообщение\s+(?:от|продавца)", text, flags=re.I)[0]
    text = text.casefold()
    subject = r"(?:^|\n)\W*(?:сделка|заявка)(?:\s+на\s+(?:покупку|продажу))?(?:\s+#[dд]\d+)?\s*(?:[:—–-]\s*)?"
    if re.search(subject + r"(?:была\s+|успешно\s+)?отменена\b", text) or re.search(
        r"(?:^|\n)\W*(?:(?:вы|продавец|покупатель)\s+отменил[аи]?\s+(?:сделку|заявку)|"
        r"статус\s*:\s*отменена)\b",
        text,
    ):
        return "CANCELLED"
    if re.search(subject + r"(?:успешно\s+)?завершена\b", text) or re.search(
        r"(?:^|\n)\W*статус\s*:\s*завершена\b", text
    ):
        return "COMPLETED"
    if re.search(subject + r"(?:была\s+)?отклонена\b", text) or re.search(
        r"(?:^|\n)\W*(?:(?:вы|продавец|покупатель)\s+отклонил[аи]?\s+(?:сделку|заявку)|"
        r"статус\s*:\s*отклонена)\b",
        text,
    ):
        return "REJECTED"
    if re.search(r"(?:вы\s+(?:отметили|подтвердили)\s+оплату|вы\s+отправили\s+плат[её]ж)", text):
        return "PAID"
    if re.search(r"(?:продавец\s+принял|сделка(?:\s+#[dд]\d+)?\s+принята)", text):
        return "ACCEPTED"
    if re.search(
        r"(?:ожида(?:йте|ние|ет).*?(?:принят|продавц)|заявка.*?создана|вы\s+создали\s+сделку)", text
    ):
        return "REQUESTED"
    return "UNKNOWN"
