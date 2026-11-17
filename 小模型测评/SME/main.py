"""Service entrypoint: `uvicorn main:app --port 6289` or `python main.py`."""
from __future__ import annotations

import uvicorn

from app.main import app

if __name__ == "__main__":
    from app.core.config import settings

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
