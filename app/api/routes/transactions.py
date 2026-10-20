"""Transaction status endpoint."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.transaction import TransactionResponse
from app.services.transaction_query_service import get_transaction

router = APIRouter(prefix="/v1/transactions", tags=["transactions"])


@router.get("/{transaction_id}", response_model=TransactionResponse)
def read_transaction(
    transaction_id: str,
    db: Session = Depends(get_db),
) -> TransactionResponse:
    transaction = get_transaction(db, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return TransactionResponse.model_validate(transaction)
