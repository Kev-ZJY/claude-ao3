"""Transport-independent read-only source contracts."""

from __future__ import annotations

from dataclasses import dataclass, field


WARNING_OPTIONS = {
    "14": "Creator Chose Not To Use Archive Warnings",
    "17": "Graphic Depictions Of Violence",
    "18": "Major Character Death",
    "16": "No Archive Warnings Apply",
    "19": "Rape/Non-Con",
    "20": "Underage Sex",
}
CATEGORY_OPTIONS = {
    "116": "F/F",
    "22": "F/M",
    "21": "Gen",
    "23": "M/M",
    "2246": "Multi",
    "24": "Other",
}
RATING_OPTIONS = {
    "9": "Not Rated",
    "10": "General Audiences",
    "11": "Teen And Up Audiences",
    "12": "Mature",
    "13": "Explicit",
}
# A supported subset of the actual AO3 Work Search select values. AO3's zh
# combines Mandarin script variants; it is not a Simplified-only selector.
LANGUAGE_OPTIONS = {"zh": "中文-普通话 國語"}

WARNING_IDS = {label: key for key, label in WARNING_OPTIONS.items()}
CATEGORY_IDS = {label: key for key, label in CATEGORY_OPTIONS.items()}
RATING_IDS = {label: key for key, label in RATING_OPTIONS.items()}


@dataclass(slots=True)
class SearchFilters:
    query: str = ""
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    rating: str = ""
    language: str = ""


@dataclass(slots=True)
class WorkSummary:
    id: str
    url: str
    title: str
    authors: list[str] = field(default_factory=list)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    rating: str = ""
    language: str = ""
    chapter_count: int | None = None
    chapter_total: int | None = None
    updated: str = ""
    language_id: str = ""


@dataclass(slots=True)
class Page:
    items: list[WorkSummary]
    url: str
    next_url: str | None = None
    previous_url: str | None = None


@dataclass(slots=True)
class SeriesRef:
    id: str
    url: str
    title: str
    position: int | None = None


@dataclass(slots=True)
class ChapterRef:
    id: str
    url: str
    title: str
    position: int


@dataclass(slots=True)
class Chapter:
    id: str
    url: str
    title: str
    paragraphs: list[str]
    work: WorkSummary
    previous_url: str | None = None
    next_url: str | None = None
    series: list[SeriesRef] = field(default_factory=list)
    position: int = 1


@dataclass(slots=True)
class WorkDetail:
    work: WorkSummary
    chapter: Chapter
    chapters: list[ChapterRef] = field(default_factory=list)
    series: list[SeriesRef] = field(default_factory=list)


@dataclass(slots=True)
class SeriesDetail:
    id: str
    url: str
    title: str
    works: list[WorkSummary]
    next_url: str | None = None
    previous_url: str | None = None


def matches_filters(work: WorkSummary, filters: SearchFilters) -> bool:
    """Apply explicit hard tags, not Any Field text, to a series continuation."""
    warnings = {WARNING_OPTIONS.get(str(x), str(x)) for x in filters.warnings}
    categories = {CATEGORY_OPTIONS.get(str(x), str(x)) for x in filters.categories}
    if not warnings.issubset(work.warnings) or not categories.issubset(work.categories):
        return False
    if filters.rating and RATING_OPTIONS.get(filters.rating, filters.rating) != work.rating:
        return False
    if filters.language:
        expected = LANGUAGE_OPTIONS.get(filters.language, filters.language)
        if filters.language != work.language_id and expected != work.language:
            return False
    return True
