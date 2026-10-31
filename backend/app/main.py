"""Application factory for the AEGIS AI backend.

Run locally from the repository root with:

    python -m uvicorn app.main:app --app-dir backend --reload
"""

from fastapi import FastAPI

from app import __version__
from app.routers import system


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Routers are registered here so the entry point stays small and each
    feature area can be developed and tested in isolation.
    """
    application = FastAPI(
        title="AEGIS AI",
        description="Autonomous Enterprise Incident Intelligence Platform",
        version=__version__,
    )
    application.include_router(system.router)
    return application


app = create_app()
