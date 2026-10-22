#!/usr/bin/env python3
"""scripts/ingest/run.py -- ingestion pipeline CLI.

Usage:
  python scripts/ingest/run.py --all
  python scripts/ingest/run.py --source nhs_cold_flu
  python scripts/ingest/run.py --status
  python scripts/ingest/run.py --query "I have a sore throat"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.ingest.chunker import chunk
from scripts.ingest.embedder import get_embedder
from scripts.ingest.fetcher import fetch
from scripts.ingest.parser import parse
from scripts.ingest.sources import SOURCE_MAP, SOURCES

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S", level=logging.INFO
)
logger = logging.getLogger("ingest")


def _bar(c, t, w=30):
    f = int(w * c / t) if t else 0
    return f"[{'#' * f}{'.' * (w - f)}] {c}/{t}"


def _dur(s):
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m {int(s % 60):02d}s"


async def ingest_source(source, embedder, store, force=False):
    t = time.monotonic()
    result = {
        "source_id": source.id,
        "title": source.title,
        "chunks": 0,
        "skipped": False,
        "error": None,
        "duration_s": 0.0,
    }
    try:
        if force:
            d = store.delete_source(source.id)
            if d:
                logger.info("  Deleted %d existing chunks for %s", d, source.id)
        logger.info("  Fetching %s ...", source.url)
        html = await fetch(source.url)
        text = parse(html, source.url)
        if not text or len(text) < 200:
            logger.warning("  Very little text (%d chars) -- skipping", len(text))
            result["skipped"] = True
            return result
        logger.info("  %d chars extracted", len(text))
        chunks = chunk(text, source_id=source.id)
        if not chunks:
            result["skipped"] = True
            return result
        logger.info("  %d chunks, embedding...", len(chunks))
        embeddings = await embedder.embed_chunks(chunks)
        n = store.upsert_chunks(chunks, embeddings, source)
        logger.info("  %d chunks upserted for %s", n, source.id)
        result["chunks"] = n
    except Exception as exc:
        logger.error("  Error: %s", exc)
        result["error"] = str(exc)
    result["duration_s"] = time.monotonic() - t
    return result


def cmd_status(store):
    indexed = store.sources_indexed()
    total = store.count()
    print(f"\n{'=' * 60}\n  {'SOURCE ID':<30} {'AUTH':<6} {'CHUNKS':>6}\n{'=' * 60}")
    if not indexed:
        print("  (no sources indexed)")
    else:
        for s in indexed:
            print(f"  {s['source_id']:<30} {s['authority']:<6} {s['chunk_count']:>6}")
    print(f"{'=' * 60}\n  Total: {total} chunks, {len(indexed)}/{len(SOURCES)} sources indexed\n")


def cmd_query(q, embedder, store, top_k=5):
    print(f"\nQuery: {q!r}\n{'-' * 60}")
    emb = embedder.embed_query(q)
    for i, r in enumerate(store.query(emb, top_k=top_k), 1):
        print(f"  [{i}] {r.title} -- score {r.score:.3f}\n      {r.url}")
        print(f"      {r.text[:200].replace(chr(10), ' ')}...")
    print()


async def main(args):
    try:
        from scripts.ingest.store import VectorStore

        store = VectorStore(
            host=os.environ.get("CHROMA_HOST", "localhost"),
            port=int(os.environ.get("CHROMA_PORT", "8001")),
        )
    except Exception as exc:
        logger.error("Cannot connect to Chroma: %s", exc)
        logger.error("Start with:  docker run -p 8001:8000 chromadb/chroma")
        sys.exit(1)
    if args.status:
        cmd_status(store)
        return
    embedder = get_embedder(force_tfidf=args.no_openai)
    if args.query:
        cmd_query(args.query, embedder, store, top_k=args.top_k)
        return
    to_index = list(SOURCES) if args.all else [SOURCE_MAP[s] for s in (args.source or [])]
    if not to_index:
        print("Specify --all, --source, --status, or --query")
        sys.exit(0)
    total = len(to_index)
    succeeded = failed = skipped = 0
    print(f"\nHandyDoctor Guideline Ingestion -- {total} sources")
    results = []
    for i, source in enumerate(to_index, 1):
        print(f"  {_bar(i - 1, total)}  {source.id}")
        r = await ingest_source(source, embedder, store, force=args.force)
        results.append(r)
        if r["error"]:
            failed += 1
        elif r["skipped"]:
            skipped += 1
        else:
            succeeded += 1
    tc = sum(r["chunks"] for r in results)
    print(f"\nSucceeded:{succeeded} Skipped:{skipped} Failed:{failed} Chunks:{tc}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--source", action="append", metavar="ID")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-openai", action="store_true", dest="no_openai")
    p.add_argument("--status", action="store_true")
    p.add_argument("--query", metavar="TEXT")
    p.add_argument("--top-k", type=int, default=5, dest="top_k")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    asyncio.run(main(args))
