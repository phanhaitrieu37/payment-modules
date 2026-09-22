"""``env:NAME`` secrets: current and previous webhook secret, optional API credential."""

from __future__ import annotations

import uuid

import pytest

from payment_module.domain.enums import ConnectionStatus, Environment, ReconcileMode
from payment_module.ports.resolvers import ProviderConnection
from payment_module.secrets import EnvSecretResolver


def connection(
    secret_ref: str = "env:SHOP_SECRET", api_credential_ref: str | None = None
) -> ProviderConnection:
    return ProviderConnection(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        merchant_id=uuid.uuid4(),
        environment=Environment.TEST,
        provider="sepay",
        locator="loc",
        status=ConnectionStatus.ACTIVE,
        reconcile_mode=ReconcileMode.DETECT_ONLY,
        timestamp_tolerance_seconds=300,
        secret_ref=secret_ref,
        api_credential_ref=api_credential_ref,
    )


async def test_current_then_previous_secret_during_rotation() -> None:
    resolver = EnvSecretResolver({"SHOP_SECRET": "new", "SHOP_SECRET_PREVIOUS": "old"})
    assert await resolver.webhook_secrets(connection()) == ["new", "old"]


async def test_only_the_current_secret_outside_rotation() -> None:
    resolver = EnvSecretResolver({"SHOP_SECRET": "new"})
    assert await resolver.webhook_secrets(connection()) == ["new"]


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
async def test_blank_values_count_as_unset(value: str) -> None:
    resolver = EnvSecretResolver({"SHOP_SECRET": value, "SHOP_SECRET_PREVIOUS": "old"})
    assert await resolver.webhook_secrets(connection()) == ["old"]


async def test_unset_secret_gives_no_secret_so_every_signature_fails() -> None:
    assert await EnvSecretResolver({}).webhook_secrets(connection()) == []


@pytest.mark.parametrize("ref", ["vault:shop/secret", "SHOP_SECRET", "env:", "env:  "])
async def test_reference_without_env_scheme_is_a_configuration_error(ref: str) -> None:
    with pytest.raises(ValueError):
        await EnvSecretResolver({"SHOP_SECRET": "x"}).webhook_secrets(connection(ref))


async def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOP_SECRET", "from-env")
    monkeypatch.delenv("SHOP_SECRET_PREVIOUS", raising=False)
    assert await EnvSecretResolver().webhook_secrets(connection()) == ["from-env"]


async def test_api_credential_is_read_from_its_own_reference() -> None:
    resolver = EnvSecretResolver({"SEPAY_API_TOKEN": "token"})
    credential = await resolver.api_credential(connection(api_credential_ref="env:SEPAY_API_TOKEN"))
    assert credential == "token"


async def test_api_credential_is_none_without_reference_or_value() -> None:
    resolver = EnvSecretResolver({"SEPAY_API_TOKEN": " "})
    assert await resolver.api_credential(connection()) is None
    for ref in ("env:SEPAY_API_TOKEN", "env:MISSING"):
        assert await resolver.api_credential(connection(api_credential_ref=ref)) is None


async def test_api_credential_reference_without_env_scheme_is_rejected() -> None:
    with pytest.raises(ValueError):
        await EnvSecretResolver({}).api_credential(connection(api_credential_ref="file:/x"))
