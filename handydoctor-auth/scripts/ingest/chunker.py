"""scripts/ingest/chunker.py -- overlapping text chunker."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    source_id: str
    chunk_index: int
    char_start: int
    char_end: int


def chunk(
    text: str,
    source_id: str,
    chunk_size: int = 1600,
    chunk_overlap: int = 320,
    min_chunk_len: int = 100,
) -> list[Chunk]:
    if not text.strip():
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    raw_chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > chunk_size:
            for sc in _split_by_sentence(para, chunk_size):
                if current:
                    raw_chunks.append(current)
                    current = ""
                raw_chunks.append(sc)
            continue
        if current and len(current) + len(para) + 2 > chunk_size:
            raw_chunks.append(current)
            current = para
        else:
            current = (current + "\n\n" + para).strip() if current else para
    if current:
        raw_chunks.append(current)
    overlapped: list[str] = []
    for i, ct in enumerate(raw_chunks):
        if i == 0:
            overlapped.append(ct)
            continue
        prev_tail = raw_chunks[i - 1][-chunk_overlap:].strip()
        overlapped.append((prev_tail + "\n\n" + ct) if prev_tail else ct)
    result: list[Chunk] = []
    char_pos = 0
    for idx, ct in enumerate(overlapped):
        if len(ct) < min_chunk_len:
            continue
        start = text.find(ct[:40], char_pos)
        if start == -1:
            start = char_pos
        result.append(
            Chunk(
                text=ct,
                source_id=source_id,
                chunk_index=idx,
                char_start=start,
                char_end=start + len(ct),
            )
        )
        char_pos = max(char_pos, start)
    return result


def _split_by_sentence(text: str, max_len: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    segments: list[str] = []
    current = ""
    for s in sentences:
        if len(s) > max_len:
            if current:
                segments.append(current)
                current = ""
            for i in range(0, len(s), max_len):
                segments.append(s[i : i + max_len])
            continue
        if current and len(current) + len(s) + 1 > max_len:
            segments.append(current)
            current = s
        else:
            current = (current + " " + s).strip() if current else s
    if current:
        segments.append(current)
    return segments
