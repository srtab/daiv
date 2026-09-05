from ninja import Router

# The GitLab/GitHub webhook callbacks live on this router — they register onto it from
# ``codebase/clients/<platform>/api/views.py``, imported by ``CodebaseConfig.ready()``.
router = Router(tags=["codebase"])
