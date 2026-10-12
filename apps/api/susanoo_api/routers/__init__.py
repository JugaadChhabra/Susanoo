"""Router mounting point.

Each owner appends their routers to their own list so merges never collide.
Every router here needs auth except /health, /demo/* (see CLAUDE.md §5) and /internal/*, which refuses
anything forwarded by Caddy so it only answers on the compose network.
"""

from fastapi import APIRouter, FastAPI

from susanoo_api.routers import (
    agent,
    approvals,
    connectors,
    demo,
    events,
    feedback,
    intake,
    internal,
    me,
    projects,
    runs,
    try_it,
)

# Jugaad: projects, connectors, demo, me, internal (Caddy's preview ask; compose network only), try_it (agent page)
PLATFORM_ROUTERS: list[APIRouter] = [
    connectors.router,
    demo.router,
    internal.router,
    me.router,
    projects.router,
    try_it.router,
]

# Muaaz: runs, events, intake, approvals, feedback, agent
AI_ROUTERS: list[APIRouter] = [
    runs.router,
    events.router,
    intake.router,
    approvals.router,
    feedback.router,
    agent.router,
]


def mount_routers(app: FastAPI) -> None:
    for router in [*PLATFORM_ROUTERS, *AI_ROUTERS]:
        app.include_router(router)
