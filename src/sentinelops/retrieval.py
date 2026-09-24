"""Exact cosine search over unit-length embeddings, with no index service."""
import numpy as np


def truncate(matrix, dimensions: int) -> np.ndarray:
    """Matryoshka truncation: the first `dimensions` values, rescaled to unit length.

    For Qwen3-Embedding this equals requesting `dimensions` from the endpoint
    (verified to 1.5e-8), so stored 1,024-d vectors can serve smaller indexes.
    """
    part = np.array(np.asarray(matrix, dtype=np.float32)[:, :dimensions])
    norms = np.linalg.norm(part, axis=1, keepdims=True)
    if part.shape[1] != dimensions or (norms == 0).any():
        raise ValueError("Cannot truncate: too few dimensions or a zero vector")
    return part / norms


def top_k(scores: np.ndarray, ids: np.ndarray, k: int):
    """Top-k (ids, scores) per row of a (queries x documents) score matrix, best first."""
    k = min(k, scores.shape[1])
    top = np.argpartition(-scores, k - 1, axis=1)[:, :k]
    order = np.argsort(-np.take_along_axis(scores, top, axis=1), axis=1, kind="stable")
    top = np.take_along_axis(top, order, axis=1)
    return ids[top], np.take_along_axis(scores, top, axis=1)


class ExactIndex:
    """Brute-force top-k by dot product; exact for unit-length rows."""

    def __init__(self, ids, matrix):
        self.ids = np.asarray(ids)
        self.matrix = np.ascontiguousarray(matrix, dtype=np.float32)
        if self.matrix.ndim != 2 or len(self.ids) != len(self.matrix) or len(np.unique(self.ids)) != len(self.ids):
            raise ValueError("Need one unique id per embedding row")
        if not np.allclose(np.linalg.norm(self.matrix, axis=1), 1.0, atol=1e-3):
            raise ValueError("Index rows must be unit length")

    @property
    def dimensions(self) -> int:
        return self.matrix.shape[1]

    @property
    def megabytes(self) -> float:
        return self.matrix.nbytes / 2**20

    def search(self, queries, k: int = 10):
        """Top-k (ids, cosine scores) per query row, best first."""
        queries = np.atleast_2d(np.asarray(queries, dtype=np.float32))
        if queries.shape[1] != self.dimensions:
            raise ValueError(f"Query has {queries.shape[1]} dimensions, index has {self.dimensions}")
        return top_k(queries @ self.matrix.T, self.ids, k)


class KeywordIndex:
    """TF-IDF cosine baseline over the same documents (sparse, exact)."""

    def __init__(self, ids, texts):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.ids = np.asarray(ids)
        self.vectorizer = TfidfVectorizer(sublinear_tf=True, stop_words="english", min_df=2, dtype=np.float32)
        self.matrix = self.vectorizer.fit_transform(texts)

    def search(self, questions: list[str], k: int = 10):
        return top_k((self.vectorizer.transform(questions) @ self.matrix.T).toarray(), self.ids, k)
