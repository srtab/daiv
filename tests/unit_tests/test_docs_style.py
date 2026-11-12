"""Style rules for the user documentation, from constitutions/patterns/user-docs.md.

Pages that still break a rule are listed in KNOWN_VIOLATIONS; migrating a page removes it from there.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pytest
from mkdocs.utils.meta import get_data

from tests.unit_tests.test_docs_links import DOCS_DIR, docs_pages, read_text

HEADING_TERMS = (
    "GitLab",
    "Docker",
    "ReviewBench",
    "GitHub",
    "Docker Compose",
    "Docker Swarm",
    "Nginx",
    "Redis",
    "PostgreSQL",
    "OpenAI",
    "Anthropic",
    "OpenRouter",
    "Google Gemini",
    "LangSmith",
    "Sentry",
    "Telegram",
    "Rocket.Chat",
    "SearXNG",
    "Tavily",
    "DuckDuckGo",
    "Claude Code",
    "Codex",
    "Request Tracker",
    "GitHub App",
    "GitHub Actions",
    "Django",
    "OAuth",
)

CHECKS = (
    "timeless",
    "reader-voice",
    "heading-case",
    "heading-shape",
    "fence-language",
    "shell-prompt",
    "placeholder-form",
    "link-text",
    "menu-path",
    "admonition-kind",
    "alt-text",
    "symbol-meaning",
)

SHELL_LANGUAGES = {"bash", "sh", "shell", "zsh", "console"}
ADMONITION_KINDS = {"note", "tip", "warning"}
BAD_LINK_TEXTS = {"here", "this", "this page", "click here", "link", "read more"}
FIRST_WORDS_BANNED = {"overview", "introduction", "summary", "general"}

FENCE_OPEN_RE = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*$")
HTML_H1_RE = re.compile(r"<h1[\s>]", re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"(`+).+?\1")
LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
REF_DEFINITION_RE = re.compile(r"^\s*\[[^\]]+\]:\s+\S+")
BARE_URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"<[^>]*>")
ATTR_LIST_RE = re.compile(r"\{[^}]*\}")
SHORTCODE_RE = re.compile(r":[a-z0-9_+-]+:")

TIMELESS_RE = re.compile(
    r"\bcurrently\b|(?<!\bas )\bsoon\b|\bnot yet\b|\bcoming\b|\bin a future (?:release|version)\b"
    r"|\bintroduced in\b|\bnew in (?:v?\d|version|release|this release|DAIV)\b"
    r"|\blatest (?:version|release)\b|\broadmap\b",
    re.IGNORECASE,
)
VOICE_RE = re.compile(r"\b(?:we|our|ours|simply|just|easy|easily|please)\b", re.IGNORECASE)
US_RE = re.compile(r"\bus\b")

PARAGRAPH_START_RE = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s|\||(?:!!!|\?\?\?|===)\s|#{1,6}\s)")
ACRONYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9.&-]*s?$")
TERMS_RE = re.compile(
    "(?<!\\w)(?:" + "|".join(re.escape(t) for t in sorted(HEADING_TERMS, key=len, reverse=True)) + ")s?(?!\\w)"
)
STEP_PREFIX_RE = re.compile(r"^(?:Step\s+\d+\b|\d+\.)", re.IGNORECASE)

PLACEHOLDER_RES = (
    re.compile(r"your[-_][a-z]"),
    re.compile(r"replace-me|changeme", re.IGNORECASE),
    re.compile(r"x{8,}", re.IGNORECASE),
    re.compile(r"YOUR_[A-Z]"),
    re.compile(r"\b[a-z0-9-]*(?:your|mycompany|my-company|acme)[a-z0-9.-]*\.[a-z]{2,}\b", re.IGNORECASE),
)
ANGLE_RE = re.compile(r"<[^>\n]*>")

LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\(")
AUTOLINK_RE = re.compile(r"<https?://[^>\s]+>")
MENU_PATH_RE = re.compile(r"\*\*[^*]+\*\*\s*(?:->|&gt;|>|»)\s*\*\*")
ADMONITION_RE = re.compile(r"^\s*(?:!!!|\?\?\?\+?)\s+(?P<kind>[\w-]+)")
EMPTY_ALT_RE = re.compile(r"!\[\s*\]\(")
IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
NONEMPTY_ALT_RE = re.compile(r"""\balt\s*=\s*(?:"[^"]*\S[^"]*"|'[^']*\S[^']*')""", re.IGNORECASE)
SYMBOL_RE = re.compile(
    r"[✅❌✔✖☑✓✗]"
    r"|(?<!\w):(?:white_check_mark|heavy_check_mark|x|material-check[\w-]*|material-close[\w-]*"
    r"|octicons-check[\w-]*|octicons-x[\w-]*):(?!\w)"
)


class Finding(NamedTuple):
    check: str
    line: int
    text: str


def _strip_code(line: str) -> str:
    return INLINE_CODE_RE.sub("C", line)


def _prose(line: str) -> str:
    """The reader-visible words of a line: no inline code, link targets, bare URLs or HTML tags."""
    line = REF_DEFINITION_RE.sub("", _strip_code(line))
    return TAG_RE.sub("", BARE_URL_RE.sub("", LINK_TARGET_RE.sub("]", line)))


def _heading_findings(lineno: int, hashes: str, raw: str, previous_level: int, findings: list[Finding]) -> None:
    level = len(hashes)
    if level > previous_level + 1:
        findings.append(Finding("heading-shape", lineno, f"level jumps from {previous_level} to {level}: {raw}"))
    text = ATTR_LIST_RE.sub("", raw).strip()
    if text.endswith((".", ":", "!", "?")):
        findings.append(Finding("heading-shape", lineno, f"closing punctuation: {raw}"))
    plain = re.sub(r"[*_]", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text))
    first = plain.split(maxsplit=1)[0].lower() if plain.split() else ""
    if first in FIRST_WORDS_BANNED:
        findings.append(Finding("heading-shape", lineno, f"empty heading word: {raw}"))
    if STEP_PREFIX_RE.match(plain):
        findings.append(Finding("heading-shape", lineno, f"numbered heading: {raw}"))

    words = SHORTCODE_RE.sub(" ", TAG_RE.sub("", INLINE_CODE_RE.sub("code", plain)))
    words = TERMS_RE.sub("term", words)
    tokens = [w for chunk in words.split() for w in chunk.split("/")]
    for token in tokens[1:]:
        word = re.sub(r"['’]s$", "", token.strip("()[]\"'“”,;:!?*_"))
        if word and word[0].isupper() and not ACRONYM_RE.match(word):
            findings.append(Finding("heading-case", lineno, f"{word!r} in: {raw}"))


def _fence_line_findings(lineno: int, language: str, line: str, findings: list[Finding]) -> None:
    if language in SHELL_LANGUAGES and line.lstrip().startswith(("$ ", "% ")):
        findings.append(Finding("shell-prompt", lineno, line.strip()))
    outside_angles = ANGLE_RE.sub("", line)
    for pattern in PLACEHOLDER_RES:
        if match := pattern.search(outside_angles):
            findings.append(Finding("placeholder-form", lineno, match.group(0)))
            break


def _prose_line_findings(
    lineno: int, line: str, following: list[str], tail: tuple[str, str], findings: list[Finding]
) -> tuple[str, str]:
    """Check one prose line, and return the (coded, prose) tail the next line of the paragraph joins to."""
    coded = _strip_code(line)
    prose = _prose(line)
    if not line.strip() or PARAGRAPH_START_RE.match(line):
        tail = ("", "")
    coded_tail, prose_tail = tail
    joined_coded = f"{coded_tail} {coded}" if coded_tail else coded
    joined_prose = f"{prose_tail} {prose}" if prose_tail else prose

    for match in TIMELESS_RE.finditer(joined_prose):
        if match.end() > len(prose_tail):
            findings.append(Finding("timeless", lineno, match.group(0)))
    for match in VOICE_RE.finditer(prose):
        findings.append(Finding("reader-voice", lineno, match.group(0)))
    for match in US_RE.finditer(prose):
        findings.append(Finding("reader-voice", lineno, match.group(0)))

    for match in LINK_RE.finditer(joined_coded):
        if match.end() <= len(coded_tail):
            continue
        text = " ".join(re.sub(r"[*_]", "", match.group("text")).lower().split())
        if text in BAD_LINK_TEXTS or text.startswith(("http://", "https://")):
            findings.append(Finding("link-text", lineno, match.group("text")))
    for match in AUTOLINK_RE.finditer(coded):
        findings.append(Finding("link-text", lineno, match.group(0)))

    for match in MENU_PATH_RE.finditer(joined_coded):
        if match.end() > len(coded_tail):
            findings.append(Finding("menu-path", lineno, match.group(0)))
    if (admonition := ADMONITION_RE.match(line)) and admonition.group("kind").lower() not in ADMONITION_KINDS:
        findings.append(Finding("admonition-kind", lineno, line.strip()))

    for match in EMPTY_ALT_RE.finditer(coded):
        findings.append(Finding("alt-text", lineno, match.group(0)))
    if "<img" in coded.lower():
        tag_source = " ".join([coded, *following])
        for match in IMG_RE.finditer(tag_source):
            if not NONEMPTY_ALT_RE.search(match.group(0)):
                findings.append(Finding("alt-text", lineno, match.group(0)))
                break

    for match in SYMBOL_RE.finditer(coded):
        findings.append(Finding("symbol-meaning", lineno, match.group(0)))
    return coded, prose


def find_style_violations(source: str) -> list[Finding]:
    body, _meta = get_data(source)
    offset = len(source.splitlines()) - len(body.splitlines())
    lines = body.splitlines()
    findings: list[Finding] = []
    fence: tuple[str, int] | None = None
    fence_language = ""
    in_comment = False
    tail = ("", "")
    previous_level = 0
    h1_count = 0

    for index, line in enumerate(lines):
        lineno = index + 1 + offset

        if fence:
            char, length = fence
            if re.match(rf"^\s*{re.escape(char)}{{{length},}}\s*$", line):
                fence = None
            else:
                _fence_line_findings(lineno, fence_language, line, findings)
            continue

        opening = FENCE_OPEN_RE.match(line)
        if opening and not (opening.group("marker")[0] == "`" and "`" in opening.group("info")):
            marker = opening.group("marker")
            fence = (marker[0], len(marker))
            info = opening.group("info").strip()
            language = info.split(maxsplit=1)[0] if info else ""
            fence_language = language.lstrip("{.").lower()
            if not info:
                findings.append(Finding("fence-language", lineno, line.strip()))
            tail = ("", "")
            continue

        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.lstrip().startswith("<!--") and "-->" not in line:
            in_comment = True
            continue

        if heading := HEADING_RE.match(line):
            hashes = heading.group("hashes")
            _heading_findings(lineno, hashes, heading.group("text"), previous_level, findings)
            previous_level = len(hashes)
            h1_count += len(hashes) == 1
        elif HTML_H1_RE.search(line):
            h1_count += 1
            previous_level = 1

        tail = _prose_line_findings(lineno, line, lines[index + 1 : index + 6], tail, findings)
        if heading:
            tail = ("", "")

    if h1_count != 1:
        findings.append(Finding("heading-shape", 1, f"{h1_count} H1 headings, expected 1"))
    return findings


BAD_SNIPPETS = [
    ("timeless", "It is currently supported."),
    ("timeless", "This arrives soon."),
    ("timeless", "Not yet available."),
    ("timeless", "# Page\n\nThis is not\nyet available."),
    ("timeless", "# Page\n\nIt ships in a future\nrelease."),
    ("timeless", "# Page\n\nNew in version 3."),
    ("link-text", "# Page\n\nClick [click\nhere](a.md)."),
    ("menu-path", "# Page\n\nSelect **Settings** >\n**Users**."),
    ("timeless", "Coming with the next update."),
    ("timeless", "Planned in a future release."),
    ("timeless", "Introduced in 2.0."),
    ("timeless", "The latest version supports it."),
    ("timeless", "See the roadmap."),
    ("reader-voice", "We recommend this."),
    ("reader-voice", "Contact us for help."),
    ("reader-voice", "Simply run the command."),
    ("reader-voice", "Please restart."),
    ("heading-case", "# Page\n\n## Configure The Server"),
    ("heading-shape", "# One\n\n# Two"),
    ("heading-shape", "# Page\n\n### Skipped"),
    ("heading-shape", "# Page\n\n## Done."),
    ("heading-shape", "# Page\n\n## Overview"),
    ("heading-shape", "# Page\n\n## Step 1 Install"),
    ("heading-shape", "# Page\n\n## 2. Install"),
    ("heading-shape", "Text without a title."),
    ("fence-language", "# Page\n\n```\nplain\n```"),
    ("fence-language", "# Page\n\n- item\n\n    ```\n    plain\n    ```"),
    ("shell-prompt", "# Page\n\n```bash\n$ make up\n```"),
    ("shell-prompt", "# Page\n\n```console\n% make up\n```"),
    ("placeholder-form", "# Page\n\n```bash\nexport KEY=your-token\n```"),
    ("placeholder-form", "# Page\n\n```bash\nexport KEY=YOUR_TOKEN\n```"),
    ("placeholder-form", "# Page\n\n```text\nsecret=xxxxxxxx\n```"),
    ("placeholder-form", "# Page\n\n```text\nhttps://daiv.mycompany.com\n```"),
    ("link-text", "Click [here](a.md)."),
    ("link-text", "[https://example.com](https://example.com)"),
    ("link-text", "See <https://example.com>."),
    ("menu-path", "Select **Settings** > **Users**."),
    ("menu-path", "Select **Settings** -> **Users**."),
    ("admonition-kind", '!!! danger "Careful"\n    Text.'),
    ("admonition-kind", "??? info\n    Text."),
    ("alt-text", "![](diagram.png)"),
    ("alt-text", '<img src="x.svg">'),
    ("alt-text", '<img src="x.svg" alt="">'),
    ("symbol-meaning", "Supported ✅"),
    ("symbol-meaning", "Supported :white_check_mark:"),
    ("symbol-meaning", "Supported :material-check-circle:"),
    ("symbol-meaning", "Unsupported :x:"),
]

GOOD_SNIPPETS = [
    "# Page\n\nOpen a new merge request.",
    "# Page\n\nThe session's latest run is shown first.",
    "# Page\n\nRuns as soon as the webhook arrives.",
    "# Page\n\nIt runs as\nsoon as the webhook arrives.",
    "# Page\n\nAnything new in the merge request is shown.",
    "# Page\n\nSee the [HTTP API](jobs-api.md) and [HTTPS setup](deployment.md).",
    "# Page\n\n- one\n- item not\n- yet another item",
    "# Page\n\n## Create Docker secrets",
    "# Page\n\n## Manage GitHub Apps",
    "# Page\n\n## Run reviews on ReviewBench",
    "# Page\n\nRun `just test` in the shell.",
    "# Page\n\nReplace `<token>` and <token> with yours.",
    "# Page\n\n## Configure Docker Compose",
    "# Page\n\n## Use the jobs API",
    "# Page\n\n## Connect GitLab and Rocket.Chat { #connect }",
    "# Page\n\n## Set the `DAIV_URL` variable",
    "# Page\n\n## Use :octicons-arrow-right-24: the CLI",
    "# Page\n\nSee [Deployment](deployment.md).",
    '# Page\n\n!!! warning "Deletes the database"\n    Back up first.',
    '# Page\n\n<img src="x.svg" alt="DAIV">',
    "# Page\n\n```bash\n# install the tool\nmake up\n```",
    "# Page\n\n```bash\nexport KEY=<your-token>\n```",
    "# Page\n\nDAIV reacts with 👀",
    '<h1 align="center">DAIV</h1>\n\n## Install',
    "---\ndescription: One sentence.\n---\n\n# Page\n\nText.",
]


@pytest.mark.parametrize(("check", "snippet"), BAD_SNIPPETS)
def test_check_flags_bad_snippet(check: str, snippet: str):
    assert check in {f.check for f in find_style_violations(snippet)}


@pytest.mark.parametrize("snippet", GOOD_SNIPPETS)
def test_check_passes_good_snippet(snippet: str):
    assert find_style_violations(snippet) == []


def test_findings_report_original_line_numbers():
    source = "---\ndescription: One sentence.\n---\n\n# Page\n\nIt is currently supported."
    assert find_style_violations(source) == [Finding("timeless", 7, "currently")]


KNOWN_VIOLATIONS: dict[str, frozenset[str]] = {
    "admonition-kind": frozenset(),
    "alt-text": frozenset(),
    "fence-language": frozenset(),
    "heading-case": frozenset({
        "customization/agent-skills.md",
        "customization/mcp-tools.md",
        "features/issue-addressing.md",
        "features/jobs-api.md",
        "features/mcp-endpoint.md",
        "features/merge-metrics.md",
        "features/notifications.md",
        "features/pipeline-watch.md",
        "features/pull-request-assistant.md",
        "features/sandbox-environments.md",
        "features/scheduled-jobs.md",
        "features/sessions.md",
        "features/slash-commands.md",
        "index.md",
        "integrations/rt/index.md",
        "reference/agent-architecture.md",
        "reference/monitoring.md",
    }),
    "heading-shape": frozenset({
        "integrations/rt/index.md",
        "reference/agent-architecture.md",
        "reference/monitoring.md",
    }),
    "link-text": frozenset(),
    "menu-path": frozenset(),
    "placeholder-form": frozenset(),
    "reader-voice": frozenset(),
    "shell-prompt": frozenset(),
    "symbol-meaning": frozenset(),
    "timeless": frozenset(),
}


def test_pages_follow_style_rules():
    failing: dict[str, dict[str, list[Finding]]] = {check: {} for check in CHECKS}
    for page in docs_pages():
        source = read_text(page)
        assert source is not None, f"{page} is not valid UTF-8"
        relative = page.relative_to(DOCS_DIR).as_posix()
        for finding in find_style_violations(source):
            failing[finding.check].setdefault(relative, []).append(finding)

    problems = []
    for check, pages in failing.items():
        baseline = KNOWN_VIOLATIONS.get(check, frozenset())
        for path, findings in sorted(pages.items()):
            if path not in baseline:
                problems.extend(f"{path}:{f.line} {check} {f.text}" for f in findings)
        problems.extend(
            f"remove {path} from KNOWN_VIOLATIONS['{check}']" for path in sorted(baseline) if path not in pages
        )
    assert not problems, "\n".join(problems)
