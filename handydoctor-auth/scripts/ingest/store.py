"""scripts/ingest/store.py -- Chroma vector store wrapper."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "nhs_guidelines")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8001"))


@dataclass
class StoredChunk:
    text: str
    source_id: str
    title: str
    authority: str
    url: str
    chunk_index: int
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


class VectorStore:
    def __init__(self, host=CHROMA_HOST, port=CHROMA_PORT):
        import chromadb

        self._client = chromadb.HttpClient(host=host, port=port)
        self._col = self._client.get_or_create_collection(
            name=CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    def upsert_chunks(self, chunks, embeddings, source) -> int:
        if not chunks:
            return 0
        self._col.upsert(
            ids=[f"{c.source_id}:{c.chunk_index}" for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[
                {
                    "source_id": c.source_id,
                    "title": source.title,
                    "authority": source.authority,
                    "url": source.url,
                    "chunk_index": c.chunk_index,
                }
                for c in chunks
            ],
        )
        return len(chunks)

    def delete_source(self, source_id):
        ids = self._col.get(where={"source_id": source_id}).get("ids", [])
        if ids:
            self._col.delete(ids=ids)
        return len(ids)

    def query(self, embedding, top_k=5, source_filter=None):
        where = {"source_id": source_filter} if source_filter else None
        n = min(top_k, self._col.count() or 1)
        res = self._col.query(
            query_embeddings=[embedding],
            n_results=n,
            include=["documents", "metadatas", "distances"],
            where=where,
        )
        return [
            StoredChunk(
                text=d,
                source_id=m.get("source_id", ""),
                title=m.get("title", ""),
                authority=m.get("authority", ""),
                url=m.get("url", ""),
                chunk_index=m.get("chunk_index", 0),
                score=max(0.0, 1.0 - dist),
                metadata=m,
            )
            for d, m, dist in zip(
                res["documents"][0], res["metadatas"][0], res["distances"][0], strict=False
            )
        ]

    def count(self):
        return self._col.count()

    def sources_indexed(self):
        try:
            metas = self._col.get(include=["metadatas"])["metadatas"]
        except Exception:
            return []
        s = {}
        for m in metas:
            sid = m.get("source_id", "")
            if sid not in s:
                s[sid] = {
                    "source_id": sid,
                    "title": m.get("title", ""),
                    "authority": m.get("authority", ""),
                    "chunk_count": 0,
                }
            s[sid]["chunk_count"] += 1
        return sorted(s.values(), key=lambda x: (x["authority"], x["source_id"]))
