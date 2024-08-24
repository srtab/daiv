from sentence_transformers import CrossEncoder


class RerankerEngine:
    MODEL_NAME = "cross-encoder/ms-marco-TinyBERT-L-2-v2"

    @classmethod
    def rerank(cls, query: str, paragraphs: list[str]) -> list[float]:
        model = CrossEncoder(cls.MODEL_NAME)
        scores = model.predict([[query, paragraph] for paragraph in paragraphs])
        return list(scores)


if __name__ == "__main__":
    query = "What is the capital of France?"
    paragraphs = ["Paris is the capital of France.", "The capital of France is Paris.", "France's capital is Paris."]
    scores = RerankerEngine.rerank(query, paragraphs)
    print(scores)  # noqa: T201
