import os

import uvicorn

from app.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    # O bind do Uvicorn falha sem derrubar ou reutilizar um processo existente.
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        # File watching duplicates the process and scans the worktree. Keep it
        # opt-in for code editing; a normal PoV run needs only one server.
        reload=os.getenv("POV_DEV", "0") == "1",
    )
