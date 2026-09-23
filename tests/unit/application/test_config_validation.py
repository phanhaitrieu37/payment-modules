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


def test_default_config_validates() -> None:
    PaymentModuleConfig().validate()


def test_largest_accepted_timing_keeps_a_bounded_positive_horizon() -> None:
    config = PaymentModuleConfig(
        reconcile_window_hours=24 * 31, reconcile_grace_seconds=7 * 24 * 3600
    )

    config.validate()
    assert timedelta(0) < config.link_horizon() <= timedelta(days=69)


def test_zero_grace_is_accepted() -> None:
    PaymentModuleConfig(reconcile_grace_seconds=0).validate()


_INVALID_INTS: list[Any] = [True, False, 1.5, "1", None]

_REJECTED: list[tuple[str, Any]] = [
    *[("reconcile_window_hours", v) for v in [0, -1, 24 * 31 + 1, *_INVALID_INTS]],
    *[("reconcile_grace_seconds", v) for v in [-1, 7 * 24 * 3600 + 1, *_INVALID_INTS]],
    *[("lease_seconds", v) for v in [0, -1, 3601, *_INVALID_INTS]],
    *[("inbox_max_attempts", v) for v in [0, -1, 1001, *_INVALID_INTS]],
    *[("outbox_max_attempts", v) for v in [0, -1, 1001, *_INVALID_INTS]],
    *[("max_body_bytes", v) for v in [0, -1, 16 * 1024 * 1024 + 1, *_INVALID_INTS]],
    *[("reconcile_page_size", v) for v in [0, -1, 1001, *_INVALID_INTS]],
    *[("late_settlement_days", v) for v in [-1, 3661, *_INVALID_INTS]],
    *[
        ("reconcile_rate_per_second", v)
        for v in [0, 0.0, -1.0, 1000.5, float("inf"), float("nan"), True, "2", None]
    ],
    *[("pii_retention_days", v) for v in [36601, True, 30.0, "30"]],
]


@pytest.mark.parametrize(("field", "value"), _REJECTED)
def test_out_of_range_or_mistyped_setting_is_rejected(field: str, value: Any) -> None:
    config = PaymentModuleConfig(**{field: value})

    with pytest.raises(InvalidConfiguration, match=field):
        config.validate()


@pytest.mark.parametrize(
    ("window_hours", "grace_seconds"), [(-1, 0), (0, -1), (-24, 900), (24, -3600)]
)
def test_timing_that_would_move_the_link_horizon_into_the_future_is_rejected(
    window_hours: int, grace_seconds: int
) -> None:
    config = PaymentModuleConfig(
        pii_retention_days=0,
        reconcile_window_hours=window_hours,
        reconcile_grace_seconds=grace_seconds,
    )

    with pytest.raises(InvalidConfiguration):
        config.validate()
