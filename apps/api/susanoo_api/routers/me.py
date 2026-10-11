"""GET /me: who the token belongs to. Lets the web app confirm a session and shows auth end to end."""

from fastapi import APIRouter

from susanoo_api.auth import CurrentUser
from susanoo_api.deps import CurrentUserDep

router = APIRouter(tags=["me"])


@router.get("/me", response_model=CurrentUser)
async def get_me(user: CurrentUserDep) -> CurrentUser:
    return user
