"""RQ worker bootstrap. Run with `python -m app.worker.main`.

This process owns a single Playwright browser instance (see app/crawler/renderer.py);
the task function creates a per-investigation BrowserContext.
"""
from __future__ import annotations

import logging
import os

import redis
import structlog
from rq import Queue, Worker

from app.config import settings


def _setup_logging() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


def main() -> None:
    _setup_logging()
    conn = redis.Redis.from_url(settings().redis_url)
    queues = [Queue("wri", connection=conn)]
    worker = Worker(queues, connection=conn, name=f"wri-worker-{os.getpid()}")
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
