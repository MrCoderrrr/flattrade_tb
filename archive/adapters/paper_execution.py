from datetime import datetime, timezone, timedelta
from itertools import count
from core_engine.execution import ExecutionAdapter
from core_engine.models import Order, Quote, Trade, Position, Side

IST = timezone(timedelta(hours=5, minutes=30))


class PaperExecution(ExecutionAdapter):
    def __init__(self):
        self._ids = count(1)
        self.orders = {}
        self.positions: dict[str, Position] = {}
        self.cash_pnl = 0.0
        self.last_quotes: dict[str, Quote] = {}

    def submit(self, order: Order, quote: Quote | None = None) -> Trade:
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")
        price = (quote.ask if order.side.value == "BUY" else quote.bid) if quote else order.limit_price
        if price is None:
            raise ValueError("paper execution requires a quote or limit_price")
        oid = f"PAPER-{next(self._ids)}"
        trade = Trade(order, float(price), quote.timestamp if quote else datetime.now(IST), oid)
        self.orders[oid] = trade
        position = self.positions.setdefault(order.symbol, Position(order.symbol))
        self.cash_pnl += position.apply(order.side, order.quantity, float(price))
        if quote:
            self.last_quotes[order.symbol] = quote
        return trade

    def mark_to_market(self, quotes: dict[str, Quote]) -> float:
        """Return realised plus unrealised paper P/L using the latest bid/ask."""
        unrealised = 0.0
        for symbol, position in self.positions.items():
            quote = quotes.get(symbol) or self.last_quotes.get(symbol)
            if quote and position.quantity:
                exit_price = quote.bid if position.quantity > 0 else quote.ask
                unrealised += position.quantity * (exit_price - position.average_price)
        return self.cash_pnl + unrealised

    def close_all(self, quotes: dict[str, Quote], strategy: str = "RiskGuardian") -> list[Trade]:
        fills = []
        for symbol, position in list(self.positions.items()):
            if position.quantity == 0:
                continue
            quote = quotes.get(symbol) or self.last_quotes.get(symbol)
            if quote is None:
                continue
            side = Side.SELL if position.quantity > 0 else Side.BUY
            order = Order(symbol, side, abs(position.quantity), strategy=strategy)
            fills.append(self.submit(order, quote))
        return fills

    def cancel(self, order_id: str) -> bool:
        return self.orders.pop(order_id, None) is not None
