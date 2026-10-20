"""Errors raised by the app."""


class RetryableProcessingError(Exception):
    """Temporary processing failure. Celery can retry it."""


class TransactionNotFoundError(Exception):
    """The transaction id is not in the database."""
