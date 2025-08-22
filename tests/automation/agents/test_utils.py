from automation.agents.utils import compute_similarity, find_original_snippet


def test_compute_similarity_exact_match():
    assert compute_similarity("hello world", "hello world") == 1.0


def test_compute_similarity_completely_different():
    assert compute_similarity("hello", "world") < 0.5


def test_compute_similarity_whitespace_handling():
    assert compute_similarity("hello   world", "helloworld", ignore_whitespace=True) == 1.0
    assert compute_similarity("hello   world", "helloworld", ignore_whitespace=False) < 1.0


def test_compute_similarity_empty_strings():
    assert compute_similarity("", "") == 1.0
    assert compute_similarity("text", "") == 0.0


def test_find_original_snippet_exact_match():
    file_contents = "def hello():\n    print('world')\n"
    snippet = "def hello():\n    print('world')"
    result = find_original_snippet(snippet, file_contents)
    assert len(result) == 1
    assert result[0].strip() == snippet


def test_find_original_snippet_empty_inputs():
    assert find_original_snippet("", "file contents") == []
    assert find_original_snippet("snippet", "") == []


def test_find_original_snippet_multiple_matches():
    file_contents = """def func1():
    print('hello')

def func2():
    print('hello')"""
    snippet = "print('hello')"
    result = find_original_snippet(snippet, file_contents, threshold=0.7)
    assert len(result) == 2


def test_find_original_snippet_no_match():
    file_contents = "def hello():\n    print('world')\n"
    snippet = "def goodbye():\n    print('earth')"
    result = find_original_snippet(snippet, file_contents)
    assert result == []


def test_find_original_snippet_threshold_sensitivity():
    file_contents = "def hello():\n    print('world')\n"
    snippet = "def helo():\n    print('world')"
    # Should match with lower threshold
    result_low = find_original_snippet(snippet, file_contents, threshold=0.7)
    assert len(result_low) > 0
    # Should not match with higher threshold
    result_high = find_original_snippet(snippet, file_contents, threshold=1)
    assert result_high == []


def test_find_original_snippet_whitespace_handling():
    file_contents = "def   hello():\n    print('world')\n"
    snippet = "def hello():\n    print('world')"
    result = find_original_snippet(snippet, file_contents)
    assert len(result) == 1
    assert result[0].strip().replace(" ", "") == snippet.replace(" ", "")
