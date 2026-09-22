"""A scripted transaction reader for reconciliation tests, written against the reader port.

``pages`` maps a provider account reference to the pages returned in order; once the script
runs out, an empty last page is returned. An item may also be a ``TransactionReadError``,
which is raised instead. ``barrier`` (optional) is awaited before each page is returned so a
test can release a webhook worker at the same moment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from payment_module.domain.account_identity import account_key
from payment_module.domain.enums import (
    Direction,
    IdentityKind,
    ObservationSource,
    ReconcileMode,
)
from payment_module.domain.money import AmountVnd
from payment_module.ports.provider import NormalizedObservation
from payment_module.ports.reader import Page, TransactionReadError, Window
from payment_module.ports.resolvers import ProviderConnection


def api_row(
    tx_id: str,
    *,
    account: str,
    bank: str = "VCB",
    sub: str | None = None,
    amount: int = 150_000,
    direction: Direction = Direction.IN,
    code: str | None = None,
    content: str | None = None,
    bank_reference: str | None = "FT0001",
) -> NormalizedObservation:
    return NormalizedObservation(
        source=ObservationSource.API,
        source_tx_id=tx_id,
        identity_kind=IdentityKind.API_ID,
        reported_account_key=account_key(bank, account, sub),
        direction=direction,
        amount=AmountVnd(amount),
        code=code,
        content=content,
        bank_reference=bank_reference,
        provider_time=None,
    )


def page(*rows: NormalizedObservation, next_cursor: str | None = None) -> Page:
    return Page(observations=tuple(rows), next_cursor=next_cursor)


@dataclass(frozen=True, slots=True)
class ReadCall:
    connection_id: object
    credential: str
    cursor: str | None
    window: Window
    account_ref: str | None


class FakeTransactionReader:
    def __init__(
        self,
        pages: Mapping[str, Sequence[Page | TransactionReadError]] | None = None,
        *,
        repeat_last: bool = False,
    ) -> None:
        self.pages = {ref: list(items) for ref, items in (pages or {}).items()}
        self.repeat_last = repeat_last
        self.calls: list[ReadCall] = []
        self.barrier: asyncio.Barrier | None = None

    def script(self, account_ref: str, *items: Page | TransactionReadError) -> None:
        self.pages.setdefault(account_ref, []).extend(items)

    async def list_page(
        self,
        connection: ProviderConnection,
        credential: str,
        cursor: str | None,
        window: Window,
        account_ref: str | None = None,
    ) -> Page:
        self.calls.append(ReadCall(connection.id, credential, cursor, window, account_ref))
        items = self.pages.get(account_ref or "", [])
        if not items:
            item: Page | TransactionReadError = page()
        elif self.repeat_last and len(items) == 1:
            item = items[0]
        else:
            item = items.pop(0)
        if self.barrier is not None:
            await self.barrier.wait()
        if isinstance(item, TransactionReadError):
            raise item
        return item


API_TOKEN = "api-token-test"


def account_ref(scope: Any) -> str:
    """The provider-side account id the tests give ``scope``'s account."""
    return f"acc-{scope.account_id.hex[:8]}"


async def enable_reconcile(
    app: Any, scope: Any, *, mode: ReconcileMode = ReconcileMode.DETECT_ONLY
) -> None:
    """Give ``scope`` an API credential, a provider account id and a reconcile mode."""
    t = app.tables
    await app.execute(
        sa.update(t.provider_connections)
        .where(t.provider_connections.c.id == scope.connection_id)
        .values(
            api_credential_ref="env:SEPAY_API_TOKEN",
            reconcile_mode=ReconcileMode(mode).value,
            reconcile_evidence_ref=(
                "evidence/sepay-test.json" if mode == ReconcileMode.AUTO_SETTLE else None
            ),
        )
    )
    await app.execute(
        sa.update(t.receiving_accounts)
        .where(t.receiving_accounts.c.id == scope.account_id)
        .values(provider_account_ref=account_ref(scope))
    )


def reconcile_module(app: Any, reader: FakeTransactionReader, **overrides: Any) -> Any:
    """A module on the app's database that reconciles connections of provider ``fake``."""
    from fakes.fake_provider import StaticSecretResolver
    from fakes.payment_app import SECRET

    values: dict[str, Any] = {
        "transaction_readers": {"fake": reader},
        "secret_resolver": StaticSecretResolver([SECRET], api_credential=API_TOKEN),
    }
    return app.build(**(values | overrides))
