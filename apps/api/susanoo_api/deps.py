"""Dependencies routers import. Every non-public route depends on `CurrentUserDep`. [PRD › Auth flow]"""

from typing import Annotated

from fastapi import Depends

from susanoo_api.auth import CurrentUser, current_user

CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
