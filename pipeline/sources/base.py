"""The common shape every fetcher returns."""
from dataclasses import dataclass
from datetime import date


@dataclass
class FetchResult:
    rows: list[tuple[date, float]]  # (period_date, value), period = first day of month/quarter/year
    vintage_date: date              # when this version of the data was published
    next_release: date | None = None
