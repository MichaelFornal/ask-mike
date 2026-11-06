"""Deterministic tune/report split.

Ported from the deal-terms-desk project's `evals/bootstrap.py` (`split_of`): sha1 of the
cluster id, first byte % 10 < 3 -> tune (about 30%), otherwise report.
"""
from __future__ import annotations

import hashlib

TUNE_TENTHS = 3


def split_of(cluster_id: str) -> str:
    bucket = hashlib.sha1(cluster_id.encode("utf-8")).digest()[0] % 10
    return "tune" if bucket < TUNE_TENTHS else "report"
