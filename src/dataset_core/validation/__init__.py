from __future__ import annotations

from dataclasses import dataclass

from dataset_core.domain import ScanIssue


@dataclass(frozen=True, slots=True)
class QualityCheckResult:
    issues: tuple[ScanIssue, ...]


def check_pairing(pairs: tuple[tuple[str, str | None], ...]) -> QualityCheckResult:
    return QualityCheckResult(
        issues=tuple(
            ScanIssue("quality.missing_annotation", "warning", image)
            for image, annotation in pairs
            if annotation is None
        )
    )
