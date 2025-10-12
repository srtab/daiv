import re


def replace_section_start_and_end_markers(log: str) -> str:
    """
    Replace section start and end markers with >>> and <<<.

    Args:
        log: Raw log content with section start and end markers

    Returns:
        Log content with section start and end markers replaced with >>> and <<<
    """
    content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_start:[0-9]*:\s*", r">>> ", log)
    content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_end:[0-9]*:\s*", r"<<< ", content)
    content = re.sub(r"section_end:[0-9]*:\s*", r"<<< ", content)

    return content


def extract_last_command_from_gitlab_logs(log: str) -> str:
    """
    Extract the output of the last executed command from the log.
    We assume that the last command is the one that failed.

    Args:
        log: Full log containing multiple commands and outputs

    Returns:
        Output of the last executed command or an empty string if no command was found
    """
    lines = log.split("\n$")
    if lines and (text := lines[-1].strip()):
        # Extract only the step_script output to avoid including other steps outputs leading to LLM hallucinations.
        # Also, add the last line to the command because it's where tipically the exit code is displayed.
        return f"$ {text.partition('<<< step_script')[0].strip()}\n{text.split('\n')[-1]}"
    return ""
