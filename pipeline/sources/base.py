"""Shared pieces for all fetchers."""
from dataclasses import dataclass
from datetime import date

# Identify ourselves honestly to data providers. Some (e.g. the Bank of England)
# block anonymous scripted requests but accept a named client.
USER_AGENT = "uk-monitoring/0.1 (personal research)"


@dataclass
class FetchResult:
    rows: list[tuple[date, float]]  # (period_date, value). Daily: the date itself. M/Q/A: first day of the period
    vintage_date: date              # when this version of the data was published (or first seen)
    next_release: date | None = None
