from automation.agents.utils import compute_similarity, extract_images_from_text, find_original_snippet


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


# Tests for extract_images_from_text


def test_extract_images_from_text_markdown_simple():
    text = "Here is an image: ![Performance Metrics](https://example.com/performance_metrics.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/performance_metrics.png"
    assert images[0].filename == "performance_metrics.png"


def test_extract_images_from_text_markdown_multiple():
    text = """
    ![Image 1](https://example.com/image1.jpg)
    Some text here
    ![Image 2](https://example.com/image2.png)
    """
    images = extract_images_from_text(text)
    assert len(images) == 2
    assert images[0].url == "https://example.com/image1.jpg"
    assert images[0].filename == "image1.jpg"
    assert images[1].url == "https://example.com/image2.png"
    assert images[1].filename == "image2.png"


def test_extract_images_from_text_html_simple():
    text = '<img src="https://example.com/auth_error_screenshot.jpg" alt="Authentication Error Screenshot">'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/auth_error_screenshot.jpg"
    assert images[0].filename == "auth_error_screenshot.jpg"


def test_extract_images_from_text_html_without_alt():
    text = '<img src="https://example.com/screenshot.png">'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/screenshot.png"
    assert images[0].filename == "screenshot.png"


def test_extract_images_from_text_html_complex_attributes():
    text = '<img width="1024" height="247" alt="Image" src="https://example.com/image.png" />'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png"
    assert images[0].filename == "image.png"


def test_extract_images_from_text_mixed_markdown_and_html():
    text = """
    ![Markdown Image](https://example.com/markdown.jpg)
    <img src="https://example.com/html.png" alt="HTML Image">
    """
    images = extract_images_from_text(text)
    assert len(images) == 2
    assert images[0].url == "https://example.com/markdown.jpg"
    assert images[0].filename == "markdown.jpg"
    assert images[1].url == "https://example.com/html.png"
    assert images[1].filename == "html.png"


def test_extract_images_from_text_with_query_parameters():
    text = "![Image](https://example.com/image.png?size=large&format=png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png?size=large&format=png"
    assert images[0].filename == "image.png"


def test_extract_images_from_text_relative_urls():
    text = "![Upload](/uploads/df8467e2dffb12ae2ca9d5f1db15cad3/screenshot.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "/uploads/df8467e2dffb12ae2ca9d5f1db15cad3/screenshot.png"
    assert images[0].filename == "screenshot.png"


def test_extract_images_from_text_no_images():
    text = "This is just regular text with no images."
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_empty_string():
    images = extract_images_from_text("")
    assert len(images) == 0


def test_extract_images_from_text_none():
    images = extract_images_from_text(None)
    assert len(images) == 0


def test_extract_images_from_text_without_valid_extensions():
    text = """
    ![No Extension](https://example.com/image)
    <img src="https://example.com/file.txt">
    """
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_github_user_attachments_without_extension():
    # GitHub user-attachments URLs should be extracted even without explicit extensions
    text = '<img src="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e" />'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"
    assert images[0].filename == "5005705d-f605-4dd7-b56c-eb513201b40e"


def test_extract_images_from_text_duplicate_urls():
    # Should not return duplicates
    text = """
    ![Image 1](https://example.com/image.png)
    ![Image 2](https://example.com/image.png)
    """
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png"


def test_extract_images_from_text_all_extensions():
    text = """
    ![JPG](https://example.com/image.jpg)
    ![JPEG](https://example.com/image.jpeg)
    ![PNG](https://example.com/image.png)
    ![GIF](https://example.com/image.gif)
    ![WEBP](https://example.com/image.webp)
    """
    images = extract_images_from_text(text)
    assert len(images) == 5
    assert images[0].url == "https://example.com/image.jpg"
    assert images[1].url == "https://example.com/image.jpeg"
    assert images[2].url == "https://example.com/image.png"
    assert images[3].url == "https://example.com/image.gif"
    assert images[4].url == "https://example.com/image.webp"


def test_extract_images_from_text_filename_from_alt_text_when_no_filename_in_url():
    # When URL has no filename, alt text should be used
    text = "![My Custom Name](https://example.com/path/to/image.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    # Filename should come from URL path, not alt text
    assert images[0].filename == "image.png"


def test_extract_images_from_text_case_insensitive_extensions():
    text = """
    ![Upper](https://example.com/image.PNG)
    ![Lower](https://example.com/image.jpg)
    """
    images = extract_images_from_text(text)
    assert len(images) == 2


def test_extract_images_from_text_github_user_attachments_with_attributes():
    # GitHub user-attachments with multiple attributes
    text = (
        '<img width="1024" height="247" alt="Image" '
        'src="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e" />'
    )
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"
    assert images[0].filename == "5005705d-f605-4dd7-b56c-eb513201b40e"


def test_extract_images_from_text_non_github_urls_without_extensions():
    # Non-GitHub URLs without extensions should still be skipped
    text = """
    ![No Extension](https://example.com/image)
    <img src="https://example.com/assets/abc123">
    """
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_github_user_attachments_security_malicious_domains():
    # Test that malicious domains with 'github.com' substring are rejected
    text = """
    ![Evil 1](https://evil-github.com/user-attachments/assets/abc123)
    ![Evil 2](https://github.com.attacker.com/user-attachments/assets/abc123)
    ![Evil 3](https://fakegithub.com/user-attachments/assets/abc123)
    ![Evil 4](https://notgithub.com/user-attachments/assets/abc123)
    """
    images = extract_images_from_text(text)
    assert len(images) == 0, "Malicious domains should be rejected"


def test_extract_images_from_text_github_user_attachments_legitimate_subdomains():
    # Test that legitimate GitHub subdomains are accepted
    text = """
    ![Valid 1](https://github.com/user-attachments/assets/abc123)
    ![Valid 2](https://private-user-images.githubusercontent.com/123/456/image.png)
    ![Valid 3](https://user-images.githubusercontent.com/user-attachments/assets/def456)
    """
    images = extract_images_from_text(text)
    # All legitimate GitHub domains should be accepted
    assert len(images) == 3
    assert images[0].url == "https://github.com/user-attachments/assets/abc123"
    assert images[1].url == "https://private-user-images.githubusercontent.com/123/456/image.png"
    assert images[2].url == "https://user-images.githubusercontent.com/user-attachments/assets/def456"
