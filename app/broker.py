from __future__ import annotations

import dramatiq
from dramatiq.middleware.prometheus import Prometheus
from dramatiq_pg import PostgresBroker

from app.config import get_settings

_configured = False


def configure_broker() -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    broker = PostgresBroker(url=settings.dramatiq_pg_url)
    broker.middleware = [middleware for middleware in broker.middleware if not isinstance(middleware, Prometheus)]
    dramatiq.set_broker(broker)
    _configured = True


configure_broker()
