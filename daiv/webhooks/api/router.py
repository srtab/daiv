from ninja import Router

from webhooks.github.views import router as github_router
from webhooks.gitlab.views import router as gitlab_router

router = Router(tags=["webhooks"])
router.add_router("/callbacks", gitlab_router)
router.add_router("/callbacks", github_router)
