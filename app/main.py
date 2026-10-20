"""FastAPI application."""

from fastapi import FastAPI

from app.api.routes import health, transactions, webhooks
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    application = FastAPI(
        title="Transaction Webhook Service",
        version="1.0.0",
        summary="Accept payment webhooks and process them in the background.",
    )
    application.include_router(health.router)
    application.include_router(webhooks.router)
    application.include_router(transactions.router)
    return application


app = create_app()
