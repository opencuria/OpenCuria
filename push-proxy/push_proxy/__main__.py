"""Run the proxy: `python -m push_proxy` (behind a TLS reverse proxy)."""

from __future__ import annotations

import logging
import os

import structlog
import uvicorn


def main() -> None:
    """Serve on PORT; trust X-Forwarded-For only from FORWARDED_ALLOW_IPS."""
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    # Provider URLs contain device tokens: never log HTTP client internals.
    for name in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    uvicorn.run(
        "push_proxy.app:create_app_from_env",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,
        server_header=False,
        log_level=logging.getLevelName(level).lower(),
    )


if __name__ == "__main__":
    main()
