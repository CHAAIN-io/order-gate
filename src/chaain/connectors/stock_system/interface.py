"""What core logic needs from a stock system, and nothing more.

The mock implements this now. A real Cin7-style connector would implement the
same two methods: list sales changed since a time, then fetch one in full,
translating the payload into `Sale`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from chaain.core.models import Sale


class StockSystem(ABC):
    @abstractmethod
    def list_sales_updated_since(self, since: datetime) -> list[Sale]:
        """Sales whose last_modified is at or after `since`, oldest first.

        Any change counts, including a payment sync, so old sales can come
        back into the window. Callers must not assume "updated" means "new".
        """

    @abstractmethod
    def get_sale(self, sale_id: str) -> Sale | None:
        """One sale in full, or None if the stock system doesn't have it."""
