"""Host migration run from Alembic: the package's frozen ``schema_v1``, then ``orders``."""

from __future__ import annotations

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from saas_host.handler import orders

from payment_module.adapters.sqlalchemy.migrations import schema_v1


def migrate(sync_conn: sa.Connection) -> None:
    op = Operations(MigrationContext.configure(sync_conn))
    schema_v1.upgrade(op)
    orders.create(sync_conn)
