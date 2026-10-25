import pytest

from automation.agent.verification.suspicious import suspicious_paths


@pytest.mark.parametrize(
    ("path", "flagged"),
    [
        ("tests/unit/test_a.py", True),
        ("src/app_test.go", True),
        ("web/a.spec.ts", True),
        ("conftest.py", True),
        ("pytest.ini", True),
        (".eslintrc.json", True),
        (".github/workflows/ci.yml", True),
        (".daiv.yml", True),
        ("src/app.py", False),
        ("pyproject.toml", False),
        ("package.json", False),
        ("docs/testing.md", False),
    ],
)
def test_suspicious_paths(path, flagged):
    assert (suspicious_paths([path]) == [path]) is flagged


def test_suspicious_paths_keeps_input_order_and_drops_the_rest():
    paths = ["src/b.py", ".gitlab-ci.yml", "src/a.py", ".circleci/config.yml", "vitest.config.ts"]

    assert suspicious_paths(paths) == [".gitlab-ci.yml", ".circleci/config.yml", "vitest.config.ts"]
