"""Payment reference generation port."""

from __future__ import annotations

from typing import Protocol

from payment_module.domain.reference import PaymentReference, ReferenceProfile


class PaymentReferenceGenerator(Protocol):
    def generate(self, profile: ReferenceProfile, prefix_name: str) -> PaymentReference:
        """Return ``profile.prefix_for(prefix_name)`` plus a fresh suffix.

        An unknown name raises ``UnknownReferencePrefix``. Uniqueness is decided by the
        database; the application retries on a collision.
        """
        ...
