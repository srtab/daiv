from ninja import Router

# The GitLab/GitHub webhook callbacks live on this router. They register onto it from
# ``webhooks/<platform>/views.py``, imported by ``WebhooksConfig.ready()``.
router = Router(tags=["codebase"])
