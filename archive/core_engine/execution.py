"""Execution contract. Live implementations can be injected without importing broker SDKs."""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from .models import Order, Quote, Trade


class ExecutionAdapter(ABC):
    @abstractmethod
    def submit(self, order: Order, quote: Quote | None = None) -> Trade: ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool: ...


class BrokerExecutionAdapter(ExecutionAdapter):
    """Optional live adapter; api_helper is imported only when instantiated."""
    def __init__(self, api=None):
        if api is None:
            from api_helper import NorenApiPy
            api = NorenApiPy()
        self.api = api

    def submit(self, order: Order, quote: Quote | None = None) -> Trade:
        # Live trading is deliberately opt-in.  A concrete method makes accidental
        # deployment fail loudly rather than silently placing a malformed order.
        raise RuntimeError("Live broker execution is disabled; use PaperExecution or configure an approved live adapter")

    def cancel(self, order_id: str) -> bool:
        return bool(self.api.cancel_order(orderno=order_id))


class FlattradeExecutionAdapter(BrokerExecutionAdapter):
    """Explicitly opt-in Flattrade boundary.

    The strategy layer only sees ``ExecutionAdapter``; live order placement is
    impossible unless ``enabled=True`` is supplied by deployment code.
    """
    def __init__(self, api=None, *, enabled: bool = False):
        super().__init__(api)
        self.enabled = enabled

    def submit(self, order: Order, quote: Quote | None = None) -> Trade:
        if not self.enabled:
            raise RuntimeError("Flattrade execution is disabled; use PaperExecution")
        placer = getattr(self.api, "place_order", None)
        if not callable(placer):
            raise RuntimeError("Flattrade API has no approved place_order interface")
        result = placer(order)
        order_id = str(result.get("norenordno", result.get("order_id", "")))
        if not order_id:
            raise RuntimeError("Flattrade rejected order without an order id")
        price = order.limit_price if order.limit_price is not None else (quote.last if quote else 0.0)
        return Trade(order, float(price), datetime.utcnow(), order_id)
