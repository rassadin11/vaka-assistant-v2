"""Serve the Telegram Mini App FastAPI backend."""

from __future__ import annotations

from typing import NoReturn

import uvicorn

from core.logging_setup import setup_logging
from webapp.settings import settings_from_env


def main() -> NoReturn:
    """Run the Mini App HTTP service."""

    setup_logging("webapp")
    settings = settings_from_env()
    uvicorn.run("webapp.app:create_app", factory=True, host="0.0.0.0", port=settings.port)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
