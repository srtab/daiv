from codebase.base import FileChange, FileChangeAction, MergeRequest


def test_is_daiv_with_bot_label():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "daiv", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_uppercase_bot_label():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "DAIV", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_bot_title():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="daiv: Automated changes",
        description="Test MR",
        labels=[],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_daiv_with_uppercase_bot_title():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="DAIV: Automated changes",
        description="Test MR",
        labels=[],
        sha="abc123",
    )
    assert mr.is_daiv() is True


def test_is_not_daiv():
    mr = MergeRequest(
        repo_id="123",
        merge_request_id=1,
        source_branch="feature",
        target_branch="main",
        title="Regular MR",
        description="Test MR",
        labels=["bug", "feature"],
        sha="abc123",
    )
    assert mr.is_daiv() is False


def test_to_markdown_create():
    file_change = FileChange(action=FileChangeAction.CREATE, file_path="src/new_file.py", content="print('hello')")
    assert file_change.to_markdown() == "Created `src/new_file.py`"


def test_to_markdown_update():
    file_change = FileChange(
        action=FileChangeAction.UPDATE, file_path="src/existing_file.py", content="print('updated')"
    )
    assert file_change.to_markdown() == "Updated `src/existing_file.py`"


def test_to_markdown_delete():
    file_change = FileChange(action=FileChangeAction.DELETE, file_path="src/old_file.py")
    assert file_change.to_markdown() == "Deleted `src/old_file.py`"


def test_to_markdown_move():
    file_change = FileChange(action=FileChangeAction.MOVE, file_path="src/new_path.py", previous_path="src/old_path.py")
    assert file_change.to_markdown() == "Renamed `src/old_path.py` to `src/new_path.py`"
