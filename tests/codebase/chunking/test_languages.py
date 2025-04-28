from pathlib import Path

from codebase.chunking.languages import filename_to_lang


def test_filename_to_lang_with_extension():
    # Test common file extensions
    assert filename_to_lang(Path("test.py")) == "python"
    assert filename_to_lang(Path("script.js")) == "javascript"
    assert filename_to_lang(Path("main.cpp")) == "cpp"
    assert filename_to_lang(Path("style.css")) == "css"
    assert filename_to_lang(Path("index.html")) == "html"


def test_filename_to_lang_with_full_filename():
    # Test special filenames without extensions
    assert filename_to_lang(Path("Dockerfile")) == "dockerfile"
    assert filename_to_lang(Path("Makefile")) == "make"
    assert filename_to_lang(Path("go.mod")) == "gomod"
    assert filename_to_lang(Path("requirements.txt")) == "requirements"


def test_filename_to_lang_with_unknown_extension():
    # Test unknown file extensions
    assert filename_to_lang(Path("unknown.xyz")) is None
    assert filename_to_lang(Path("file.unknown")) is None


def test_filename_to_lang_with_no_extension():
    # Test files without extensions
    assert filename_to_lang(Path("README")) is None
    assert filename_to_lang(Path("LICENSE")) is None


def test_filename_to_lang_with_multiple_dots():
    # Test files with multiple dots in the name
    assert filename_to_lang(Path("test.min.js")) == "javascript"
    assert filename_to_lang(Path("config.prod.json")) == "json"


def test_filename_to_lang_with_case_sensitivity():
    # Test case sensitivity
    assert filename_to_lang(Path("test.PY")) == "python"
    assert filename_to_lang(Path("TEST.py")) == "python"
    assert filename_to_lang(Path("Test.Py")) == "python"
