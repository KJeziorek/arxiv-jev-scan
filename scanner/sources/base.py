"""The interface every paper source implements. Adding a new source (bioRxiv,
OpenReview, ...) means adding one file with a class like ArxivSource."""

from abc import ABC, abstractmethod
from datetime import date

from scanner.models import Paper

__all__ = ["Paper", "PaperSource"]


class PaperSource(ABC):
    name: str  # stored with each paper and scan run, e.g. "arxiv"

    @abstractmethod
    def fetch_day(self, categories: list[str], day: date) -> list[Paper]:
        """Papers from `categories` that appeared or changed on `day`.

        The pipeline asks for one day at a time, which keeps memory use flat
        during big backfills and gives it a natural checkpoint after each day.
        """
