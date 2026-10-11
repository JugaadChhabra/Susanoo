"""Router mounting point.

Each owner appends their routers to their own list so merges never collide.
Every router here needs auth except /health, /demo/* (see CLAUDE.md §5).
"""

from fastapi import APIRouter, FastAPI

from susanoo_api.routers import connectors, demo, me, projects

# Jugaad: projects, connectors, demo, me
PLATFORM_ROUTERS: list[APIRouter] = [connectors.router, demo.router, me.router, projects.router]

# Muaaz: runs, events, intake, approvals, feedback, agent
AI_ROUTERS: list[APIRouter] = []


def mount_routers(app: FastAPI) -> None:
    for router in [*PLATFORM_ROUTERS, *AI_ROUTERS]:
        app.include_router(router)
