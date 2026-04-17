from ninja import Schema


class UserSearchResult(Schema):
    id: int
    username: str
    name: str
    email: str
