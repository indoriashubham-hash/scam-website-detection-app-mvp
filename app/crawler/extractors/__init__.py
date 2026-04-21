"""Pluggable page-level extractors. Each extractor is a small, pure function.

Order matters a little: metadata and visible-text run first because later extractors
read from their outputs; everything else is independent.
"""
from __future__ import annotations

from app.crawler.extractors.base import ExtractContext, ExtractorResult, run_pipeline
from app.crawler.extractors.bot_block import extract_bot_block
from app.crawler.extractors.contact import extract_contacts
from app.crawler.extractors.forms import extract_forms
from app.crawler.extractors.language import extract_language
from app.crawler.extractors.legal import extract_legal_pages
from app.crawler.extractors.links import extract_links
from app.crawler.extractors.metadata import extract_metadata
from app.crawler.extractors.trackers import extract_trackers
from app.crawler.extractors.visible_text import extract_visible_text

# Ordered pipeline. Keep this list as the single source of truth.
# bot_block runs right after metadata so downstream extractors can read
# `extracted["bot_block"]` and decide whether to skip their own work.
DEFAULT_PIPELINE = (
    extract_metadata,
    extract_bot_block,
    extract_visible_text,
    extract_language,
    extract_forms,
    extract_links,
    extract_contacts,
    extract_legal_pages,
    extract_trackers,
)

__all__ = [
    "ExtractContext",
    "ExtractorResult",
    "DEFAULT_PIPELINE",
    "run_pipeline",
]
