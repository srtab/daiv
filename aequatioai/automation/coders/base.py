import os
import re

from aequatioai.automation.agents.agent import LlmAgent
from aequatioai.automation.agents.models import Message
from aequatioai.automation.agents.prompts import EditBlockPrompts


HEAD = "<<<<<<< SEARCH"
DIVIDER = "======="
UPDATED = ">>>>>>> REPLACE"

separators = "|".join([HEAD, DIVIDER, UPDATED])

split_re = re.compile(r"^((?:" + separators + r")[ ]*\n)", re.MULTILINE | re.DOTALL)

missing_filename_err = (
    "Bad/missing filename. The filename must be alone on the line before the opening fence"
    " {fence[0]}"
)

class RefactorCoder:
    fence = "<source>", "</source>"
    
    def __init__(self, root_directory: str) -> None:
        self.root_directory = os.path.dirname(root_directory)

    def invoke(self, prompt: str, files_to_change: list[str], refactor_example: str | None = None):
        memory = [
            Message(role="system", content=EditBlockPrompts.format_main_system(fence=self.fence)),
            Message(role="user", content=EditBlockPrompts.format_files_to_change(files_content=self.get_files_content(files_to_change))),
            Message(role="assistant", content="Ok."),
            Message(role="user", content=prompt),
        ]
        if refactor_example:
            memory.append(Message(role="user", content=EditBlockPrompts.format_refactor_example(self.get_files_content([refactor_example]))))

        self.agent = LlmAgent(memory=memory)
        response = self.agent.run()
        
        for filename, original_text, updated_text in self.find_original_update_blocks(response):
            print("## filename:", filename)
            print("## original_text:\n", original_text)
            print("## updated_text:\n", updated_text)
        return response
            
    def get_files_content(self, files_to_change: list[str]) -> str:
        prompt = ""
        for fname in files_to_change:
            with open(fname) as f:
                content = f.read()
            relative_fname = os.path.relpath(fname, self.root_directory)
            prompt += f"\n{relative_fname}\n{self.fence[0]}\n{content}{self.fence[1]}\n"
        return prompt

    def find_original_update_blocks(self, content):
        # make sure we end with a newline, otherwise the regex will miss <<UPD on the last line
        if not content.endswith("\n"):
            content = content + "\n"

        pieces = re.split(split_re, content)

        pieces.reverse()
        processed = []

        # Keep using the same filename in cases where GPT produces an edit block without a filename.
        current_filename = None
        try:
            while pieces:
                cur = pieces.pop()

                if cur in (DIVIDER, UPDATED):
                    processed.append(cur)
                    raise ValueError(f"Unexpected {cur}")

                if cur.strip() != HEAD:
                    processed.append(cur)
                    continue

                processed.append(cur)  # original_marker

                filename = self.strip_filename(processed[-2].splitlines()[-1])
                try:
                    if not filename:
                        filename = self.strip_filename(processed[-2].splitlines()[-2])
                    if not filename:
                        if current_filename:
                            filename = current_filename
                        else:
                            raise ValueError(missing_filename_err.format(fence=self.fence))
                except IndexError:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(missing_filename_err.format(fence=self.fence))

                current_filename = filename

                original_text = pieces.pop()
                processed.append(original_text)

                divider_marker = pieces.pop()
                processed.append(divider_marker)
                if divider_marker.strip() != DIVIDER:
                    raise ValueError(f"Expected `{DIVIDER}` not {divider_marker.strip()}")

                updated_text = pieces.pop()
                processed.append(updated_text)

                updated_marker = pieces.pop()
                processed.append(updated_marker)
                if updated_marker.strip() != UPDATED:
                    raise ValueError(f"Expected `{UPDATED}` not `{updated_marker.strip()}")

                yield filename, original_text, updated_text
        except ValueError as e:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ {e.args[0]}")
        except IndexError:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ Incomplete SEARCH/REPLACE block.")
        except Exception:
            processed = "".join(processed)
            raise ValueError(f"{processed}\n^^^ Error parsing SEARCH/REPLACE block.")

    def strip_filename(self, filename):
        filename = filename.strip()

        if filename == "...":
            return

        if filename.startswith(self.fence[0]):
            return

        filename = filename.rstrip(":")
        filename = filename.strip("`")

        return filename
    

if __name__ == "__main__":
    coder = RefactorCoder(root_directory="/home/sfr/work/bankinter/repos/bkcf_onboarding/")
    response = coder.invoke(prompt="Integrate debugpy package to the Django manage.py file.", files_to_change=["/home/sfr/work/bankinter/repos/bkcf_onboarding/bkcf_onboarding/manage.py"], refactor_example="/home/sfr/work/inov/repos/feedportal/feedportal/manage.py")
    print(response)