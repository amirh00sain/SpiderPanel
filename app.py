"""ASGI entry point for Python application hosts.

Configure hosts that accept an ASGI target to run ``app:app``. The project
uses port 8080 by default; platforms that start this file directly may set
``PORT`` to the port assigned to the service.
"""

from __future__ import annotations

import os

import uvicorn

import main as spider_main

app = spider_main.app


def _configured_port() -> int:
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT must be between 1 and 65535, got {port}")
    spider_main.PANEL_PORT = port
    spider_main.CONFIG["port"] = port
    return port


# ASGI hosts import ``app:app`` instead of executing this file as a script.
_configured_port()


def main() -> None:
    port = _configured_port()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
