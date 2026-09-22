"""Optional FastAPI adapter; needs the ``fastapi`` extra."""

from payment_module.adapters.fastapi.router import make_webhook_router

__all__ = ["make_webhook_router"]
