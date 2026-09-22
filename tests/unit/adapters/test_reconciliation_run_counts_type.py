"""A finished run's counts carry more than integers: the ids of rows the reader could not
read are stored next to the tallies, so both the port and its adapter accept JSON values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import get_type_hints

import pytest

from payment_module.adapters.sqlalchemy.repositories.reconciliation_runs import (
    SqlAlchemyReconciliationRunRepository,
)
from payment_module.domain.events import JsonValue
from payment_module.ports.unit_of_work import ReconciliationRunRepository


@pytest.mark.parametrize(
    "repository", [ReconciliationRunRepository, SqlAlchemyReconciliationRunRepository]
)
def test_finish_counts_accept_json_values(repository: type) -> None:
    assert get_type_hints(repository.finish)["counts"] == Mapping[str, JsonValue]


def test_json_values_include_lists_of_ids() -> None:
    assert list[JsonValue] in JsonValue.__value__.__args__
