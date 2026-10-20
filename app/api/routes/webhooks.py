"""Transaction webhook endpoint."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.webhook import WebhookAcknowledgement, WebhookRequest
from app.services.webhook_service import accept_transaction

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

_STORAGE_ERRORS = (OperationalError, DisconnectionError, InterfaceError)


@router.post(
    "/transactions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAcknowledgement,
)
def receive_transaction_webhook(
    payload: WebhookRequest,
    db: Session = Depends(get_db),
) -> WebhookAcknowledgement:
    """Save the webhook and return. The worker does the slow part later."""
    try:
        transaction, is_duplicate = accept_transaction(db, payload)
    except _STORAGE_ERRORS as exc:
        logger.exception("database failure while accepting a webhook")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistent storage is temporarily unavailable",
        ) from exc

    if is_duplicate:
        return WebhookAcknowledgement(
            status="duplicate",
            transaction_id=transaction.transaction_id,
            message="Transaction already received",
        )
    return WebhookAcknowledgement(
        status="accepted",
        transaction_id=transaction.transaction_id,
        message="Transaction accepted for processing",
    )
