"""An in-memory stock system for tests and the demo.

Fill it with sales from the generator, then let the workflow poll it exactly
as it would poll the real system.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chaain.connectors.stock_system.interface import StockSystem
from chaain.core.models import Sale


class MockStockSystem(StockSystem):
    def __init__(self, sales: list[Sale] | tuple[Sale, ...] = ()) -> None:
        self._sales: dict[str, Sale] = {}
        self.add(*sales)

    def add(self, *sales: Sale) -> None:
        for sale in sales:
            self._sales[sale.id] = sale

    def update(self, sale_id: str, modified_at: datetime, **changes) -> Sale:
        """Change a sale the way staff or a sync would, bumping last_modified.

        With no other changes this mimics a payment sync: nothing about the
        order changes, but it reappears in the next poll's window.
        """
        sale = replace(self._sales[sale_id], last_modified=modified_at, **changes)
        self._sales[sale_id] = sale
        return sale

    def list_sales_updated_since(self, since: datetime) -> list[Sale]:
        found = [s for s in self._sales.values() if s.last_modified >= since]
        return sorted(found, key=lambda s: s.last_modified)

    def get_sale(self, sale_id: str) -> Sale | None:
        return self._sales.get(sale_id)
