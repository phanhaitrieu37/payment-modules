"""Use cases of the payment module. Each takes only the ports it uses.

Use cases open their own unit of work per transaction boundary; ``CreateIntent`` and
``CancelIntent`` can instead run inside a unit of work joined to the host's transaction.
"""

from __future__ import annotations

from collections.abc import Mapping

from payment_module.ports.provider import PaymentProvider


def provider_for(providers: Mapping[str, PaymentProvider], code: str) -> PaymentProvider:
    """The registered provider for a connection, or ``LookupError`` for a wiring mistake."""
    try:
        return providers[code]
    except KeyError:
        raise LookupError(f"no payment provider registered for {code!r}") from None
