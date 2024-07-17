from textwrap import dedent

from instructor import OpenAISchema
from pydantic import Field

from automation.agents.tools import FunctionTool
from automation.coders.base import STOP_MESSAGE
from automation.coders.tools import CodeActionTools

# Use this tool to ask questions if you are unable to address the notes or the notes is ambiguous.


class RequestFeedback(OpenAISchema):
    """
    This tool is used to ask for feedback if you are unable to address the comments or the comments are ambiguous.

    ### Examples ###
    1.
    User: How are you?
    Question: I am unable to understand the comment. Can you give more context about the intended changes?

    2.
    User: Change the name of the function.
    Question: Please provide the name of the function you would like me to change.
    """

    questions: list[str] = Field(
        description=dedent(
            """\
            Questions for the user to answer to help you complete the task.
            """
        )
    )


class ReviewAddressorTools(CodeActionTools):
    """
    Tools to perform code review addressor actions.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.questions = []

    def request_feedback(self, questions: list[str]):
        self.questions.extend(questions)
        return STOP_MESSAGE

    def get_tools(self):
        tools = super().get_tools()
        tools.append(FunctionTool(schema_model=RequestFeedback, fn=self.request_feedback))
        return tools
