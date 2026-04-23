from __future__ import annotations

import uvicorn

from jarvis.config.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "jarvis.api.app:create_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
