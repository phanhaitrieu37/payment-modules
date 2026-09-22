"""Secret resolvers the host can plug into the module."""

from payment_module.secrets.env_secret_resolver import EnvSecretResolver

__all__ = ["EnvSecretResolver"]
