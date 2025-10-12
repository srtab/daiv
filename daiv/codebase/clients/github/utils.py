import re

# Matches:
# 2025-09-22T21:40:35Z
# 2025-09-22T21:40:35.4116534Z
# 2025-09-22T21:40:35+01:00
# 2025-09-22T21:40:35.4116534+01:00
_TS_PREFIX = re.compile(
    r"(?m)^\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})\s+"
)


def strip_iso_timestamps(text: str) -> str:
    """
    Remove the datetime prefix at the start of each log line.
    """
    return _TS_PREFIX.sub("", text)


def extract_last_command_from_github_logs(log: str) -> str:
    """
    Clean GitHub logs by removing irrelevant information and extracting the last failed command.

    Args:
        log: Full log containing multiple commands and outputs

    Returns:
        Output of the last executed command or an empty string if no command was found
    """
    # Find the failure line (non-zero exit)
    m_fail = re.search(r"##\[error\]Process completed with exit code (\d+)\.", log)

    if not m_fail or int(m_fail.group(1)) == 0:
        return ""

    fail_start = m_fail.start()

    # Last "##[group]Run ..." before the failure
    groups = list(re.finditer(r"##\[group\]Run .+", log))

    start_matches = [g for g in groups if g.start() < fail_start]
    if not start_matches:
        return ""

    start = start_matches[-1].end() + 1  # +1 to skip the newline

    # Capture until next group header or "Post job cleanup" or end
    next_boundary = re.search(r"(?:##\[group\]|Post job cleanup)", log[start + 1 :])
    end = (start + 1 + next_boundary.start()) if next_boundary else len(log)

    return log[start:end].rstrip()
