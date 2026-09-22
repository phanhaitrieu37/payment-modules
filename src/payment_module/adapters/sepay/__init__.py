"""SePay adapter: webhook provider, API v2 transaction reader and template checklist.

Hosts register it explicitly, for example
``build_payment_module(..., provider_registry={"sepay": SePayProvider()},
transaction_readers={"sepay": SePayTransactionReader()})``. The reader needs the ``sepay``
extra (``httpx``).
"""

from __future__ import annotations

from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider

__all__ = ["SePayProvider", "SePayTemplateChecklist", "SePayTransactionReader"]


def __getattr__(name: str) -> object:
    # The reader imports httpx, an optional dependency; load it only when asked for.
    if name == "SePayTransactionReader":
        from payment_module.adapters.sepay.reader import SePayTransactionReader

        return SePayTransactionReader
    raise AttributeError(name)
