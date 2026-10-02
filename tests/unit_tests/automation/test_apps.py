from codebase.base import MergeRequest, User
from core.checkpointer import DAIVRedisSerializer


def test_ready_lets_a_pre_deploy_merge_request_checkpoint_revive():
    """The exact envelope ``DAIVRedisSerializer`` wrote while ``core`` hard-coded ``MergeRequest``."""
    envelope = {
        "lc": 2,
        "type": "constructor",
        "id": ["codebase.base", "MergeRequest"],
        "kwargs": {
            "repo_id": "srtab/daiv2",
            "merge_request_id": 30,
            "source_branch": "feat/x",
            "target_branch": "main",
            "title": "t",
            "description": "d",
            "labels": ["daiv"],
            "web_url": None,
            "sha": None,
            "author": {"id": 2, "name": "DAIV", "username": "daiv"},
            "draft": False,
            "merged": False,
        },
    }

    revived = DAIVRedisSerializer()._revive_if_needed(envelope)

    assert revived == MergeRequest(
        repo_id="srtab/daiv2",
        merge_request_id=30,
        source_branch="feat/x",
        target_branch="main",
        title="t",
        description="d",
        labels=["daiv"],
        author=User(id=2, name="DAIV", username="daiv"),
    )
