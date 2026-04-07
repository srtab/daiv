from ninja import Schema


class RepositorySearchResult(Schema):
    slug: str
    name: str
