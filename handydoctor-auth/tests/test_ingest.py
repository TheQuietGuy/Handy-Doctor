"""tests/test_ingest.py -- tests for the NHS guideline ingestion pipeline."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.ingest.chunker import chunk
from scripts.ingest.embedder import TFIDFEmbedder, get_embedder
from scripts.ingest.parser import _clean, parse
from scripts.ingest.sources import SOURCE_MAP, SOURCES


class TestSourcesCatalogue:
    def test_has_minimum_sources(self):
        assert len(SOURCES) >= 20

    def test_all_sources_have_required_fields(self):
        for s in SOURCES:
            assert s.id and s.url and s.title and s.authority and s.topics

    def test_ids_are_unique(self):
        ids = [s.id for s in SOURCES]
        assert len(ids) == len(set(ids))

    def test_urls_are_https(self):
        for s in SOURCES:
            assert s.url.startswith("https://")

    def test_authorities_are_known(self):
        valid = {"NHS", "NICE", "WHO", "BTS"}
        for s in SOURCES:
            assert s.authority in valid

    def test_source_map_matches_sources(self):
        assert len(SOURCE_MAP) == len(SOURCES)
        for s in SOURCES:
            assert SOURCE_MAP[s.id] is s

    def test_nhs_sources_present(self):
        assert len([s for s in SOURCES if s.authority == "NHS"]) >= 10

    def test_nice_sources_present(self):
        assert len([s for s in SOURCES if s.authority == "NICE"]) >= 4

    def test_who_sources_present(self):
        assert len([s for s in SOURCES if s.authority == "WHO"]) >= 1


class TestParser:
    def test_extracts_text_from_simple_html(self):
        html = b"<html><body><main><p>NHS recommends rest and fluids.</p></main></body></html>"
        result = parse(html)
        assert "NHS" in result and "rest" in result

    def test_removes_nav_elements(self):
        html = b"<html><body><nav>Skip to content</nav><main><p>Clinical advice: paracetamol for fever.</p></main><footer>Back to top</footer></body></html>"
        result = parse(html)
        assert "Clinical advice" in result
        assert "Skip to content" not in result

    def test_removes_script_tags(self):
        html = b"<html><body><script>console.log(1);</script><main><p>Take ibuprofen with food.</p></main></body></html>"
        result = parse(html)
        assert "console.log" not in result
        assert "ibuprofen" in result

    def test_strips_boilerplate_phrases(self):
        raw = (
            "NHS recommends rest.\nBack to top\nPage last reviewed: 2024\nThis helps with recovery."
        )
        cleaned = _clean(raw)
        assert "Back to top" not in cleaned
        assert "Page last reviewed" not in cleaned
        assert "NHS recommends rest." in cleaned

    def test_empty_html_returns_short_string(self):
        result = parse(b"<html><body></body></html>")
        assert len(result) < 10

    def test_handles_bytes_decoding(self):
        result = parse(b"NHS recommends rest.")
        assert isinstance(result, str)

    def test_collapses_whitespace(self):
        cleaned = _clean("Word1   Word2\t\tWord3 long enough line here")
        assert "  " not in cleaned
        assert "\t" not in cleaned

    def test_drops_very_short_lines(self):
        raw = "OK\nNHS recommends adequate rest and hydration during illness."
        cleaned = _clean(raw)
        assert cleaned.strip().startswith("NHS")


_SAMPLE = (
    "NHS recommends rest and adequate fluid intake for colds and flu.\n\n"
    "Paracetamol (500 to 1000 mg every 4 to 6 hours) can relieve fever and aches. "
    "Ibuprofen is an alternative for adults.\n\n"
    "Seek medical attention if symptoms worsen significantly after three days, "
    "you develop chest pain, difficulty breathing, or a high fever above 39C "
    "that does not respond to paracetamol.\n\n"
    "Antibiotics are not effective against viral infections and are not recommended."
)


class TestChunker:
    def test_returns_chunks_for_valid_text(self):
        assert len(chunk(_SAMPLE, "test_source")) >= 1

    def test_chunks_have_content(self):
        for c in chunk(_SAMPLE, "test_source"):
            assert c.text.strip()

    def test_chunks_have_correct_source_id(self):
        for c in chunk(_SAMPLE, "my_source"):
            assert c.source_id == "my_source"

    def test_chunk_indices_are_sequential(self):
        indices = [c.chunk_index for c in chunk(_SAMPLE, "test_source")]
        assert indices == sorted(indices)

    def test_empty_text_returns_no_chunks(self):
        assert chunk("", "empty") == []
        assert chunk("   \n\n   ", "blank") == []

    def test_chunk_size_respected(self):
        for c in chunk(_SAMPLE, "test_source", chunk_size=200, chunk_overlap=40):
            assert len(c.text) <= 600

    def test_overlap_present_between_consecutive_chunks(self):
        chunks = chunk(_SAMPLE, "test_source", chunk_size=200, chunk_overlap=60)
        if len(chunks) < 2:
            return
        tail = chunks[0].text[-30:].strip()
        assert tail in chunks[1].text or len(chunks) == 1

    def test_long_paragraph_is_split(self):
        long_para = " ".join(["word"] * 500)
        assert len(chunk(long_para, "long_para", chunk_size=400)) > 1

    def test_single_short_text_gives_one_chunk(self):
        text = "NHS recommends rest and adequate fluid intake. Paracetamol can help relieve fever and aches in adults and children."
        assert len(chunk(text, "short", chunk_size=400)) == 1

    def test_chunk_dataclass_fields(self):
        c = chunk(_SAMPLE, "ds_test")[0]
        assert isinstance(c.text, str)
        assert isinstance(c.source_id, str)
        assert isinstance(c.chunk_index, int)
        assert isinstance(c.char_start, int)
        assert isinstance(c.char_end, int)


class _FakeChunk:
    def __init__(self, text, idx=0):
        self.text = text
        self.source_id = "test"
        self.chunk_index = idx
        self.char_start = 0
        self.char_end = len(text)


class TestTFIDFEmbedder:
    @pytest.mark.asyncio
    async def test_produces_embeddings(self):
        emb = TFIDFEmbedder(max_features=50)
        vecs = await emb.embed_chunks([_FakeChunk("NHS recommends rest for cold and flu.")])
        assert len(vecs) == 1
        assert len(vecs[0]) == 50

    @pytest.mark.asyncio
    async def test_embedding_dimension_matches(self):
        emb = TFIDFEmbedder(max_features=200)
        chunks = [_FakeChunk("paracetamol fever headache"), _FakeChunk("back pain sciatica")]
        vecs = await emb.embed_chunks(chunks)
        assert all(len(v) == 200 for v in vecs)

    @pytest.mark.asyncio
    async def test_one_embedding_per_chunk(self):
        emb = TFIDFEmbedder(max_features=50)
        chunks = [_FakeChunk(f"sample text chunk {i}") for i in range(7)]
        vecs = await emb.embed_chunks(chunks)
        assert len(vecs) == 7

    @pytest.mark.asyncio
    async def test_different_texts_give_different_vectors(self):
        emb = TFIDFEmbedder(max_features=100)
        chunks = [
            _FakeChunk("chest pain cardiac emergency"),
            _FakeChunk("cold flu sore throat runny nose"),
        ]
        vecs = await emb.embed_chunks(chunks)
        assert vecs[0] != vecs[1]

    def test_embed_query_after_fit(self):
        import asyncio

        emb = TFIDFEmbedder(max_features=50)
        asyncio.run(emb.embed_chunks([_FakeChunk("training text for fitting vectoriser")]))
        vec = emb.embed_query("I have a headache")
        assert len(vec) == 50
        assert isinstance(vec[0], float)

    def test_embed_query_before_fit_raises(self):
        emb = TFIDFEmbedder(max_features=50)
        with pytest.raises(RuntimeError, match="fitted"):
            emb.embed_query("query before fit")

    def test_get_embedder_returns_tfidf_without_key(self):
        import os

        with patch.dict("os.environ", {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            embedder = get_embedder()
        assert isinstance(embedder, TFIDFEmbedder)

    def test_get_embedder_force_tfidf(self):
        assert isinstance(get_embedder(force_tfidf=True), TFIDFEmbedder)

    @pytest.mark.asyncio
    async def test_empty_chunks_list(self):
        emb = TFIDFEmbedder(max_features=50)
        assert await emb.embed_chunks([]) == []


class TestIngestPipeline:
    _SAMPLE_HTML = (
        b"<html><body><main>"
        b"<h1>Cold and flu self-care</h1>"
        b"<p>NHS recommends rest and adequate fluid intake for cold and flu symptoms.</p>"
        b"<p>Paracetamol (500 to 1000 mg every 4 to 6 hours, maximum 4 grams per day) "
        b"can relieve fever and aches. Antibiotics are not recommended.</p>"
        b"<p>Seek medical attention if symptoms worsen after three days, or you develop "
        b"chest pain, difficulty breathing, or a high fever above 39 degrees Celsius.</p>"
        b"</main></body></html>"
    )

    @pytest.mark.asyncio
    async def test_ingest_source_happy_path(self):
        from scripts.ingest.run import ingest_source

        source = SOURCE_MAP["nhs_cold_flu"]
        embedder = TFIDFEmbedder(max_features=50)
        await embedder.embed_chunks([_FakeChunk("training text for fitting")])
        mock_store = MagicMock()
        mock_store.delete_source = MagicMock(return_value=0)
        mock_store.upsert_chunks = MagicMock(return_value=3)
        with patch(
            "scripts.ingest.run.fetch", new_callable=AsyncMock, return_value=self._SAMPLE_HTML
        ):
            result = await ingest_source(source, embedder, mock_store, force=False)
        assert result["error"] is None
        assert result["chunks"] > 0
        assert not result["skipped"]
        mock_store.upsert_chunks.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_source_empty_page_is_skipped(self):
        from scripts.ingest.run import ingest_source

        source = SOURCE_MAP["nhs_cold_flu"]
        embedder = TFIDFEmbedder(max_features=50)
        mock_store = MagicMock()
        with patch(
            "scripts.ingest.run.fetch",
            new_callable=AsyncMock,
            return_value=b"<html><body><p>OK</p></body></html>",
        ):
            result = await ingest_source(source, embedder, mock_store)
        assert result["skipped"] is True
        mock_store.upsert_chunks.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_source_fetch_error_recorded(self):
        from scripts.ingest.run import ingest_source

        source = SOURCE_MAP["nhs_cold_flu"]
        embedder = TFIDFEmbedder(max_features=50)
        mock_store = MagicMock()
        with patch(
            "scripts.ingest.run.fetch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Network error"),
        ):
            result = await ingest_source(source, embedder, mock_store)
        assert result["error"] is not None
        assert "Network" in result["error"]
        mock_store.upsert_chunks.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_flag_deletes_existing(self):
        from scripts.ingest.run import ingest_source

        source = SOURCE_MAP["nhs_cold_flu"]
        embedder = TFIDFEmbedder(max_features=50)
        mock_store = MagicMock()
        mock_store.delete_source = MagicMock(return_value=5)
        mock_store.upsert_chunks = MagicMock(return_value=3)
        with patch(
            "scripts.ingest.run.fetch", new_callable=AsyncMock, return_value=self._SAMPLE_HTML
        ):
            await ingest_source(source, embedder, mock_store, force=True)
        mock_store.delete_source.assert_called_once_with(source.id)
