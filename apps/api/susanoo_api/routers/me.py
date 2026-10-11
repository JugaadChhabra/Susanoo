"""GET /me: who the token belongs to. Lets the web app confirm a session and shows auth end to end.

GET /me/quota: runs left today, for the projects screen. [PRD › Frontend and API › Screens › Projects]
"""

from fastapi import APIRouter

from susanoo_api.auth import CurrentUser
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import RunQuota
from susanoo_api.quota import QuotaStoreDep, current_quota

router = APIRouter(tags=["me"])


@router.get("/me", response_model=CurrentUser)
async def get_me(user: CurrentUserDep) -> CurrentUser:
    return user


@router.get("/me/quota", response_model=RunQuota)
async def get_my_quota(user: CurrentUserDep, store: QuotaStoreDep) -> RunQuota:
    return await current_quota(user.id, store)
