"""Main-body text extraction. Tries trafilatura (higher quality on article-style
pages); falls back to readability-lxml; falls back to soup.get_text()."""
from __future__ import annotations

import hashlib

from simhash import Simhash

from app.crawler.extractors.base import ExtractContext, ExtractorResult


def _shingles(text: str, n: int = 4) -> list[str]:
    words = text.split()
    if len(words) < n:
        return [text]
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def extract_visible_text(ctx: ExtractContext) -> ExtractorResult:
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(ctx.html, include_comments=False, include_tables=False) or ""
    except Exception:
        text = ""
    if not text:
        try:
            from readability import Document

            doc = Document(ctx.html)
            text = Document(doc.summary()).summary()  # fallback just to get some body text
        except Exception:
            text = ""
    if not text:
        text = ctx.soup.get_text(" ", strip=True)

    text = " ".join(text.split())
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    # Simhash returns a 64-bit UNSIGNED integer (0..2^64-1), but our Postgres
    # `simhash` column is BIGINT (signed, -2^63..2^63-1). About half of all
    # hashes would overflow. Coerce to signed-int64 via two's-complement so
    # the bit pattern is preserved; `_simhash_hamming()` in the pipeline
    # re-masks before XOR so distance math stays correct.
    sh_raw = Simhash(_shingles(text)).value if text else None
    sh = _u64_to_i64(sh_raw) if sh_raw is not None else None
    wc = len(text.split()) if text else 0

    return ExtractorResult(
        extracted={
            "readable_text": text[:200_000],    # cap to keep Postgres JSONB reasonable
            "readable_text_len": wc,
            "content_hash": sha,
            "simhash": sh,
        }
    )


def _u64_to_i64(v: int) -> int:
    """Map an unsigned 64-bit int into signed 64-bit two's-complement range.

    Needed because Postgres BIGINT is signed; the simhash library returns an
    unsigned 64-bit value. The round-trip preserves all 64 bits, so XOR-based
    hamming distance is unaffected as long as the caller re-masks to uint64
    before XOR.
    """
    if v >= (1 << 63):
        return v - (1 << 64)
    return v
