"""Data source fetchers.

Each source module exposes fetch(series) -> FetchResult, where `series` is one
row of series.csv. The pipeline looks up the right fetcher by the `source`
column, so adding a source means writing one module and adding it here.
"""
from pipeline.sources import boe, ons

FETCHERS = {
    "ONS": ons.fetch,
    "BoE": boe.fetch,
}
