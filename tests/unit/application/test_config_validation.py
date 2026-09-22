"""A retention shorter than the cross-source link horizon would purge the memo that links a
later sighting of the same money, so the module refuses to be built with one."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from payment_module.application.config import InvalidConfiguration, PaymentModuleConfig
from payment_module.builder import build_payment_module


def test_link_horizon_is_two_read_windows_and_the_grace() -> None:
    config = PaymentModuleConfig(reconcile_window_hours=6, reconcile_grace_seconds=600)

    assert config.link_horizon() == timedelta(hours=12, seconds=600)


@pytest.mark.parametrize("days", [None, 3, 30])
def test_retention_covering_the_link_horizon_is_accepted(days: int | None) -> None:
    PaymentModuleConfig(pii_retention_days=days).validate()


@pytest.mark.parametrize("days", [0, 1, 2])
def test_retention_shorter_than_the_link_horizon_is_rejected(days: int) -> None:
    with pytest.raises(InvalidConfiguration, match="link horizon"):
        PaymentModuleConfig(pii_retention_days=days).validate()


def test_negative_retention_is_rejected() -> None:
    with pytest.raises(InvalidConfiguration, match="negative"):
        PaymentModuleConfig(pii_retention_days=-1).validate()


def test_retention_is_checked_against_the_configured_window() -> None:
    with pytest.raises(InvalidConfiguration):
        PaymentModuleConfig(pii_retention_days=3, reconcile_window_hours=48).validate()


def test_builder_refuses_a_retention_shorter_than_the_link_horizon() -> None:
    unused: Any = None

    with pytest.raises(InvalidConfiguration):
        build_payment_module(
            PaymentModuleConfig(pii_retention_days=0),
            uow_factory=unused,
            provider_registry={},
            secret_resolver=unused,
            clock=unused,
        )
