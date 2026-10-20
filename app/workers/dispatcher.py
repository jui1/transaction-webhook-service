"""Poll the transactional outbox and publish due work to Celery."""

import logging
import signal
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_sessionmaker
from app.services.outbox_dispatcher import dispatch_once
from app.workers.publisher import CeleryTaskPublisher

logger = logging.getLogger(__name__)
_stop = False


def _request_stop(signum: int, frame: object) -> None:
    global _stop
    logger.info("dispatcher received signal %s", signum)
    _stop = True


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    settings = get_settings()
    publisher = CeleryTaskPublisher()
    logger.info("outbox dispatcher started")
    while not _stop:
        try:
            published = dispatch_once(get_sessionmaker(), publisher)
            if published == 0:
                time.sleep(settings.outbox_poll_interval_seconds)
        except Exception:
            logger.exception("outbox dispatch iteration failed")
            time.sleep(settings.outbox_poll_interval_seconds)
    logger.info("outbox dispatcher stopped")


if __name__ == "__main__":
    main()
