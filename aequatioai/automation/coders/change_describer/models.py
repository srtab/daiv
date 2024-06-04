from pydantic import BaseModel


class ChangesDescription(BaseModel):
    branch: str
    title: str
    description: str
    commit_message: str
