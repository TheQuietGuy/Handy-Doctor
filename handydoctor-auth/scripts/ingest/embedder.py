"""scripts/ingest/embedder.py -- OpenAI + TF-IDF embedders."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)
OPENAI_BATCH_SIZE = 100
OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_EMBED_DIMS = 1536


class BaseEmbedder(ABC):
    @property
    @abstractmethod
    def dimension(self) -> int: ...
    @abstractmethod
    async def embed_chunks(self, chunks) -> list[list[float]]: ...
    @abstractmethod
    def embed_query(self, query: str) -> list[float]: ...


class OpenAIEmbedder(BaseEmbedder):
    def __init__(self, api_key=None, model=OPENAI_EMBED_MODEL):
        from openai import AsyncOpenAI, OpenAI

        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        self._async = AsyncOpenAI(api_key=key)
        self._sync = OpenAI(api_key=key)
        self._model = model

    @property
    def dimension(self):
        return OPENAI_EMBED_DIMS

    async def embed_chunks(self, chunks) -> list[list[float]]:
        if not chunks:
            return []
        embeddings = []
        texts = [c.text for c in chunks]
        for i in range(0, len(texts), OPENAI_BATCH_SIZE):
            resp = await self._async.embeddings.create(
                model=self._model, input=texts[i : i + OPENAI_BATCH_SIZE]
            )
            embeddings.extend(item.embedding for item in sorted(resp.data, key=lambda x: x.index))
        return embeddings

    def embed_query(self, query):
        return self._sync.embeddings.create(model=self._model, input=[query]).data[0].embedding


class TFIDFEmbedder(BaseEmbedder):
    def __init__(self, max_features=8000):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self._vec = TfidfVectorizer(
            max_features=max_features, ngram_range=(1, 2), sublinear_tf=True, stop_words="english"
        )
        self._fitted = False
        self._dim = max_features

    @property
    def dimension(self):
        return self._dim

    async def embed_chunks(self, chunks) -> list[list[float]]:
        if not chunks:
            return []
        texts = [c.text for c in chunks]
        if not self._fitted:
            self._vec.fit(texts)
            self._fitted = True
        matrix = self._vec.transform(texts).toarray()
        result = []
        for row in matrix:
            v = row.tolist()
            if len(v) < self._dim:
                v += [0.0] * (self._dim - len(v))
            result.append(v[: self._dim])
        return result

    def embed_query(self, query):
        if not self._fitted:
            raise RuntimeError("TFIDFEmbedder must be fitted before querying")
        v = self._vec.transform([query]).toarray()[0].tolist()
        if len(v) < self._dim:
            v += [0.0] * (self._dim - len(v))
        return v[: self._dim]


def get_embedder(force_tfidf=False) -> BaseEmbedder:
    if force_tfidf:
        return TFIDFEmbedder()
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        logger.info("Using OpenAI embedder")
        return OpenAIEmbedder(api_key=key)
    logger.warning("OPENAI_API_KEY not set -- using TF-IDF embedder")
    return TFIDFEmbedder()
