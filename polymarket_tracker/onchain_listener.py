"""Быстрая полоса: сделки прямо из блокчейна, на пять минут раньше Data API.

Зачем
-----
Data API публикует сделки пачками раз в пять минут, поэтому наш поток
отстаёт от реального времени на 250-380 секунд. Замер показал, чего это
стоит:

    группа                        первые 5 мин   через час
    все выигравшие                    +1.3%        +15.5%
    "рынок побежал" (n=142)          +15.9%        +64.2%

В общей массе опоздание почти ничего не стоит. Но в той группе, ради
которой всё делается, за первые пять минут уходит четверть движения:
войти на пять минут раньше — значит войти на 16% дешевле.

Голова цепочки при этом отстаёт от реального времени на ~1 секунду
(замерено на подписке: 2739 событий за 45 секунд).

Почему это ОТДЕЛЬНАЯ полоса, а не замена Data API
-------------------------------------------------
Слушатель НИЧЕГО не пишет в таблицу trades. Причина техническая и важная:
у события в логе нет готовой отметки времени сделки, а если подставить
текущее время, то одна и та же сделка окажется в базе на пять минут
"моложе", чем её же запись из Data API. От этого поедут чекпоинт,
почасовые базовые обороты и сторож простоя.

Поэтому разделение такое:
  * блокчейн — ранний сигнал, ничего не пишет в поток сделок;
  * Data API — система учёта: полные метаданные, дедупликация, история.
Если вебсокет отвалится, трекер продолжит работать ровно как раньше,
просто без форы во времени.

Что именно слушаем
------------------
Событие OrderFilled обоих контрактов V2 (ABI снят с Etherscan):

    OrderFilled(bytes32 indexed orderHash, address indexed maker,
                address indexed taker, uint8 side, uint256 tokenId,
                uint256 makerAmountFilled, uint256 takerAmountFilled,
                uint256 fee, bytes32 builder, bytes32 metadata)

Расшифровка сверена с Data API по шести сделкам: сумма, цена и токен
совпали во всех случаях.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import aiohttp

log = logging.getLogger(__name__)

# Определено по структуре события, а не по хешу: у OrderFilled три
# индексных поля (4 темы) и семь слов данных. Проверено на живом потоке —
# из трёх типов событий контракта только это имеет такую форму.
ORDER_FILLED_PREFIX = "0xd543adfd"
ORDER_FILLED_TOPICS = 4
ORDER_FILLED_WORDS = 7

# У USDC и у долей исхода по шесть знаков после запятой.
DECIMALS = 1_000_000

RECONNECT_BASE_SEC = 3.0
RECONNECT_MAX_SEC = 60.0


@dataclass(frozen=True)
class OnchainTrade:
    """Сделка, расшифрованная из лога. Отдельный тип от Trade намеренно:
    здесь нет метаданных рынка и нет отметки времени самой сделки."""

    tx_hash: str
    log_index: int
    block_number: int
    maker: str
    taker: str
    side: str          # "buy" | "sell" — сторона МЕЙКЕРА
    token_id: str
    usdc_amount: float
    shares: float
    price: float
    seen_at: float     # когда МЫ это увидели

    def __str__(self) -> str:
        return (f"OnchainTrade[{self.side.upper()}] {self.maker[:10]} "
                f"${self.usdc_amount:,.0f} @ {self.price:.3f}")


def decode_order_filled(log_entry: dict, seen_at: Optional[float] = None) -> Optional[OnchainTrade]:
    """Расшифровать лог OrderFilled. None — если это не он или форма чужая.

    Цена считается из двух сумм, а не берётся готовой: при side=BUY мейкер
    отдаёт USDC и получает доли, при SELL наоборот. Отсюда и направление.
    """
    topics = log_entry.get("topics") or []
    if len(topics) != ORDER_FILLED_TOPICS:
        return None
    if not str(topics[0]).lower().startswith(ORDER_FILLED_PREFIX):
        return None

    data = str(log_entry.get("data") or "")[2:]
    if len(data) < ORDER_FILLED_WORDS * 64:
        return None
    words = [int(data[i * 64:(i + 1) * 64], 16) for i in range(ORDER_FILLED_WORDS)]
    side_raw, token_id, maker_amount, taker_amount = words[0], words[1], words[2], words[3]

    if maker_amount <= 0 or taker_amount <= 0:
        return None  # пустое исполнение — считать цену не из чего

    if side_raw == 0:            # мейкер покупает: отдал USDC, получил доли
        usdc, shares = maker_amount, taker_amount
        side = "buy"
    else:                        # мейкер продаёт: отдал доли, получил USDC
        usdc, shares = taker_amount, maker_amount
        side = "sell"

    price = usdc / shares
    if not (0 < price <= 1):
        return None  # доля бинарного исхода не может стоить дороже доллара

    try:
        block_number = int(log_entry.get("blockNumber", "0x0"), 16)
        log_index = int(log_entry.get("logIndex", "0x0"), 16)
    except (TypeError, ValueError):
        block_number, log_index = 0, 0

    return OnchainTrade(
        tx_hash=str(log_entry.get("transactionHash", "")),
        log_index=log_index,
        block_number=block_number,
        maker="0x" + str(topics[2])[-40:],
        taker="0x" + str(topics[3])[-40:],
        side=side,
        token_id=str(token_id),
        usdc_amount=usdc / DECIMALS,
        shares=shares / DECIMALS,
        price=price,
        seen_at=seen_at if seen_at is not None else time.time(),
    )


class OnchainListener:
    """Подписка на OrderFilled обоих контрактов через вебсокет."""

    def __init__(self, wss_url: str, addresses: list, min_usdc: float = 0.0):
        self.wss_url = wss_url
        self.addresses = [a.lower() for a in addresses]
        self.min_usdc = min_usdc
        self._session: Optional[aiohttp.ClientSession] = None
        self.stats = {"events": 0, "decoded": 0, "yielded": 0, "reconnects": 0}

    async def start(self) -> None:
        """Поднять сессию. Владеем ей явно, а не создаём внутри генератора:
        при отмене задачи генератор остаётся висеть, и сессия закрывается
        только сборщиком мусора — с руганью в лог."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=90)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def stream(self) -> AsyncIterator[OnchainTrade]:
        """Бесконечный поток сделок. Переподключается сам."""
        backoff = RECONNECT_BASE_SEC
        while True:
            try:
                async for trade in self._stream_once():
                    backoff = RECONNECT_BASE_SEC  # соединение живое — сбрасываем паузу
                    yield trade
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — обрыв связи не должен ронять трекер
                self.stats["reconnects"] += 1
                log.warning(
                    "Ончейн-подписка оборвалась (%s), переподключаюсь через %.0f с",
                    type(e).__name__, backoff,
                )
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX_SEC, backoff * 2)

    async def _stream_once(self) -> AsyncIterator[OnchainTrade]:
        if self._session is None or self._session.closed:
            await self.start()
        async with self._session.ws_connect(self.wss_url, heartbeat=30) as ws:
            for i, address in enumerate(self.addresses):
                await ws.send_json({
                    "jsonrpc": "2.0", "id": i + 1, "method": "eth_subscribe",
                    "params": ["logs", {"address": address}],
                })
            log.info("Ончейн-подписка оформлена на %d контракта(ов)", len(self.addresses))

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                entry = payload.get("params", {}).get("result")
                if not entry:
                    continue  # подтверждение подписки или посторонний ответ
                self.stats["events"] += 1
                trade = decode_order_filled(entry)
                if trade is None:
                    continue
                self.stats["decoded"] += 1
                if trade.usdc_amount < self.min_usdc:
                    continue
                self.stats["yielded"] += 1
                yield trade
