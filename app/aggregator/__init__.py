"""Risk Aggregator — turns raw evidence rows into a verdict.

The aggregator is *deterministic*. It does not call an LLM and does not read from
the network; given the same evidence list it always returns the same Verdict.
This is intentional: the verdict is the product's core safety claim and must be
auditable and unit-testable. LLMs (if/when added) write narrative *on top* of the
verdict, never instead of it.
"""
from __future__ import annotations

from app.aggregator.rules import Finding, Verdict, aggregate

__all__ = ["Finding", "Verdict", "aggregate"]
