from ninja import Router

# The GitLab/GitHub webhook callbacks live on this router — they register onto it from
# ``codebase/clients/<platform>/api/views.py``, which is why it declares no routes itself.
router = Router(tags=["codebase"])
