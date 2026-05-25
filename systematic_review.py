#!/usr/bin/env python3
"""Systematic literature review pipeline with PRISMA tracking.

Multi-database search → deduplication → title/abstract screening →
full-text eligibility → PRISMA flowchart export.

All data stored in SQLite (Python stdlib). Zero external dependencies.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT_BASE = "SystematicReviewBot/1.0"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

PRISMA_STAGES = ["identified", "deduplicated", "screened", "eligible", "included"]
PRISMA_EXCLUDE_REASONS = [
    "duplicate",
    "wrong_topic",
    "wrong_population",
    "wrong_methodology",
    "not_empirical",
    "not_peer_reviewed",
    "no_full_text",
    "language_barrier",
    "out_of_date_range",
    "other",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def clean_text(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value if item)
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_doi(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    doi = value.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi.strip()


def doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}" if doi else ""


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def user_agent(mailto: str = "") -> str:
    if mailto:
        return f"{USER_AGENT_BASE} (mailto:{mailto})"
    return USER_AGENT_BASE


def http_json(url: str, *, mailto: str = "", retries: int = 3, delay: float = 0.6, extra_headers: dict[str, str] | None = None) -> Any:
    headers = {"User-Agent": user_agent(mailto), "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    last_error: str | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} for {url}"
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(delay * attempt)
                continue
            raise RuntimeError(last_error) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(delay * attempt)
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or f"Failed to fetch {url}")


def http_text(url: str, *, mailto: str = "", retries: int = 3, delay: float = 0.6) -> str:
    headers = {"User-Agent": user_agent(mailto), "Accept": "application/atom+xml,text/xml,*/*"}
    last_error: str | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} for {url}"
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(delay * attempt)
                continue
            raise RuntimeError(last_error) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(delay * attempt)
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or f"Failed to fetch {url}")


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    command TEXT NOT NULL,
    config_snapshot TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT DEFAULT '',
    title TEXT NOT NULL,
    abstract TEXT DEFAULT '',
    authors TEXT DEFAULT '',
    year INTEGER,
    journal TEXT DEFAULT '',
    source_database TEXT NOT NULL,
    source_query TEXT DEFAULT '',
    url TEXT DEFAULT '',
    pdf_url TEXT DEFAULT '',
    publication_type TEXT DEFAULT 'journal-article',
    raw_metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stages (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
    stage TEXT NOT NULL DEFAULT 'identified',
    exclusion_reason TEXT DEFAULT '',
    exclusion_detail TEXT DEFAULT '',
    screened_by TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicates (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    duplicate_of_id INTEGER NOT NULL REFERENCES papers(id),
    match_method TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (paper_id, duplicate_of_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source_database);
CREATE INDEX IF NOT EXISTS idx_stages_stage ON stages(stage);
CREATE INDEX IF NOT EXISTS idx_stages_reason ON stages(exclusion_reason);
"""


def get_db(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(DB_SCHEMA)
    return db


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, config_path: str | None = None) -> None:
        raw = read_json(Path(config_path), {}) if config_path else {}
        self.databases: dict[str, bool] = raw.get("databases", {})
        self.keyword_groups: list[dict[str, Any]] = raw.get("keyword_groups", [])
        self.date_from: str = raw.get("date_from", "2015-01-01")
        self.date_until: str = raw.get("date_until", utc_now().date().isoformat())
        self.max_per_query: int = int(raw.get("max_per_query", 100))
        self.max_per_database: int = int(raw.get("max_per_database", 1500))
        self.sleep: float = float(raw.get("sleep", 0.5))
        self.mailto: str = raw.get("crossref_mailto", "")
        self.output_dir: str = raw.get("output_dir", "systematic-review")
        self.language: str = raw.get("language", "zh-CN")
        self.auto_screen_keywords: bool = bool(raw.get("auto_screen_keywords", False))
        self.required_terms: list[str] = raw.get("required_in_title_abstract", [])

    @property
    def all_terms(self) -> list[str]:
        return sorted({term for group in self.keyword_groups for term in group["terms"]})

    @property
    def databases_enabled(self) -> list[str]:
        return [name for name, enabled in self.databases.items() if enabled]


# ---------------------------------------------------------------------------
# Database connectors
# ---------------------------------------------------------------------------

def search_semantic_scholar(term: str, cfg: Config, offset: int = 0) -> list[dict[str, Any]]:
    """Search Semantic Scholar API. Free, no key required (100 req/5min)."""
    params = {
        "query": term,
        "limit": min(cfg.max_per_query, 100),
        "offset": offset,
        "fields": "title,abstract,authors,year,doi,url,externalIds,publicationVenue,openAccessPdf,publicationTypes",
    }
    # Apply date filter via year parameter range
    if cfg.date_from:
        year_from = cfg.date_from[:4]
        params["year"] = f"{year_from}-"
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    data = http_json(url, mailto=cfg.mailto)
    papers: list[dict[str, Any]] = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        if not title:
            continue
        doi = normalize_doi(item.get("externalIds", {}).get("DOI"))
        authors_list = item.get("authors", [])
        authors = "; ".join(a.get("name", "") for a in authors_list if isinstance(a, dict)) if authors_list else ""
        venue = item.get("publicationVenue") or {}
        abstract = clean_text(item.get("abstract"))
        pub_types = item.get("publicationTypes", [])
        pub_type = pub_types[0] if pub_types else "unknown"
        pdf_info = item.get("openAccessPdf") or {}
        papers.append({
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "year": item.get("year"),
            "journal": clean_text(venue.get("name")),
            "url": doi_url(doi) or item.get("url", ""),
            "pdf_url": pdf_info.get("url", ""),
            "publication_type": pub_type,
            "source_database": "semantic_scholar",
            "source_query": term,
        })
        if len(papers) >= cfg.max_per_query:
            break
    return papers


def search_openalex(term: str, cfg: Config, page: int = 1) -> list[dict[str, Any]]:
    """Search OpenAlex API. Free, no key required (polite pool)."""
    per_page = min(cfg.max_per_query, 200)
    params = {
        "search": term,
        "per-page": str(per_page),
        "page": str(page),
        "sort": "cited_by_count:desc",
        "filter": "type:article",
    }
    if cfg.date_from:
        params["filter"] += f",from_publication_date:{cfg.date_from}"
    if cfg.date_until:
        params["filter"] += f",to_publication_date:{cfg.date_until}"
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = http_json(url, mailto=cfg.mailto)
    papers: list[dict[str, Any]] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        if not title:
            continue
        doi = normalize_doi(item.get("doi"))
        authors_list = item.get("authorships", [])
        authors_parts = []
        for a in authors_list:
            au = a.get("author", {}) if isinstance(a, dict) else {}
            name = clean_text(au.get("display_name"))
            if name:
                authors_parts.append(name)
        abstract = inverted_abstract_index(item.get("abstract_inverted_index"))
        loc = item.get("primary_location") or {}
        src = loc.get("source") or {}
        papers.append({
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": "; ".join(authors_parts),
            "year": item.get("publication_year"),
            "journal": clean_text(src.get("display_name")),
            "url": doi_url(doi) or loc.get("landing_page_url", ""),
            "pdf_url": loc.get("pdf_url", ""),
            "publication_type": clean_text(item.get("type")),
            "source_database": "openalex",
            "source_query": term,
        })
    return papers


def inverted_abstract_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in index.items():
        if not isinstance(indexes, list):
            continue
        for position in indexes:
            if isinstance(position, int):
                positions.append((position, word))
    positions.sort(key=lambda pair: pair[0])
    return clean_text(" ".join(word for _, word in positions))


def search_eric(term: str, cfg: Config, start: int = 0) -> list[dict[str, Any]]:
    """Search ERIC (Education Resources Information Center) API. Free, no key."""
    params = {
        "search": term,
        "format": "json",
        "rows": min(cfg.max_per_query, 100),
        "start": str(start),
        "fields": "title,author,abstract,descriptor,sponsoringagency,publicationdate,year,database,source,documenttype,identifier",
    }
    url = "https://api.ies.ed.gov/eric/?" + urllib.parse.urlencode(params)
    data = http_json(url, mailto=cfg.mailto)
    papers: list[dict[str, Any]] = []
    docs = data.get("response", {}).get("docs", [])
    for item in docs:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        if not title:
            continue
        abstract = clean_text(item.get("abstract"))
        authors_raw = item.get("author")
        if isinstance(authors_raw, list):
            authors = "; ".join(clean_text(a) for a in authors_raw)
        else:
            authors = clean_text(authors_raw)
        pub_date = clean_text(item.get("publicationdate"))
        year = int(pub_date[:4]) if len(pub_date) >= 4 else None
        identifier = item.get("identifier") or {}
        doi = ""
        if isinstance(identifier, dict):
            doi = normalize_doi(identifier.get("doi"))
        elif isinstance(identifier, list):
            for ident in identifier:
                d = normalize_doi(ident)
                if d:
                    doi = d
                    break
        papers.append({
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "year": year,
            "journal": clean_text(item.get("source")),
            "url": doi_url(doi),
            "pdf_url": "",
            "publication_type": clean_text(item.get("database")),
            "source_database": "eric",
            "source_query": term,
        })
    return papers


def search_crossref(term: str, cfg: Config) -> list[dict[str, Any]]:
    """Search Crossref API. Uses existing mailto configuration."""
    params: dict[str, str] = {
        "query.bibliographic": term,
        "filter": "type:journal-article",
        "rows": str(min(cfg.max_per_query, 100)),
        "sort": "published",
        "order": "desc",
    }
    if cfg.date_from:
        params["filter"] += f",from-pub-date:{cfg.date_from}"
    if cfg.date_until:
        params["filter"] += f",until-pub-date:{cfg.date_until}"
    if cfg.mailto:
        params["mailto"] = cfg.mailto
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data = http_json(url, mailto=cfg.mailto)
    papers: list[dict[str, Any]] = []
    raw_items = data.get("message", {}).get("items", [])
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        if not title:
            continue
        doi = normalize_doi(item.get("DOI"))
        authors_raw = item.get("author", [])
        authors_parts = []
        for a in authors_raw:
            given = clean_text(a.get("given"))
            family = clean_text(a.get("family"))
            name = " ".join(p for p in [given, family] if p)
            if name:
                authors_parts.append(name)
        pub_parts = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
        date_parts = pub_parts.get("date-parts", [[None]])[0] if pub_parts else [None]
        year = int(date_parts[0]) if date_parts and date_parts[0] else None
        papers.append({
            "doi": doi,
            "title": title,
            "abstract": clean_text(item.get("abstract")),
            "authors": "; ".join(authors_parts),
            "year": year,
            "journal": clean_text(item.get("container-title")),
            "url": doi_url(doi) or clean_text(item.get("URL")),
            "pdf_url": "",
            "publication_type": "journal-article",
            "source_database": "crossref",
            "source_query": term,
        })
    return papers


def search_arxiv(term: str, cfg: Config) -> list[dict[str, Any]]:
    """Search arXiv API."""
    params = {
        "search_query": f'all:"{term}"',
        "start": "0",
        "max_results": str(min(cfg.max_per_query, 100)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = ARXIV_API + "?" + urllib.parse.urlencode(params)
    try:
        xml_text = http_text(url, mailto=cfg.mailto)
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    papers: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", namespaces=ARXIV_NS):
        title = clean_text(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
        if not title:
            continue
        abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
        entry_url = clean_text(entry.findtext("atom:id", default="", namespaces=ARXIV_NS))
        arxiv_id = entry_url.rsplit("/", 1)[-1]
        authors_list = []
        for author in entry.findall("atom:author", namespaces=ARXIV_NS):
            name = clean_text(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
            if name:
                authors_list.append(name)
        published_raw = clean_text(entry.findtext("atom:published", default="", namespaces=ARXIV_NS))
        year = None
        if published_raw:
            try:
                year = int(published_raw[:4])
            except ValueError:
                pass
        papers.append({
            "doi": "",
            "title": title,
            "abstract": abstract,
            "authors": "; ".join(authors_list),
            "year": year,
            "journal": "arXiv preprint",
            "url": entry_url or f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "publication_type": "preprint",
            "source_database": "arxiv",
            "source_query": term,
        })
    return papers


SEARCHERS = {
    "semantic_scholar": search_semantic_scholar,
    "openalex": search_openalex,
    "eric": search_eric,
    "crossref": search_crossref,
    "arxiv": search_arxiv,
}


# ---------------------------------------------------------------------------
# Title / abstract similarity for dedup
# ---------------------------------------------------------------------------

def title_similarity(a: str, b: str) -> float:
    """Simple token-based similarity for fuzzy title matching."""

    def tokens(s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]{3,}", s.lower()))

    ta = tokens(a)
    tb = tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def insert_paper(db: sqlite3.Connection, paper: dict[str, Any]) -> tuple[int | None, bool]:
    """Insert a paper or return existing ID. Deduplicates by DOI then title similarity.

    Returns (paper_id, is_new) tuple.
    """
    doi = paper.get("doi", "")
    title = paper.get("title", "")

    # Check by DOI first (only non-empty)
    if doi:
        cur = db.execute("SELECT id FROM papers WHERE doi = ? AND doi != ''", (doi,))
        row = cur.fetchone()
        if row:
            return row["id"], False

    # Check by title similarity (fuzzy dedup)
    cur = db.execute("SELECT id, title FROM papers WHERE title LIKE ?", (f"%{title[:80]}%",))
    for row in cur.fetchall():
        if title_similarity(title, row["title"]) > 0.85:
            return row["id"], False

    try:
        cur = db.execute(
            """INSERT INTO papers (doi, title, abstract, authors, year, journal,
               source_database, source_query, url, pdf_url, publication_type, raw_metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doi,
                title,
                paper.get("abstract", ""),
                paper.get("authors", ""),
                paper.get("year"),
                paper.get("journal", ""),
                paper.get("source_database", ""),
                paper.get("source_query", ""),
                paper.get("url", ""),
                paper.get("pdf_url", ""),
                paper.get("publication_type", ""),
                json.dumps(paper, ensure_ascii=False),
                utc_now().isoformat(),
            ),
        )
        db.commit()
        return cur.lastrowid, True
    except sqlite3.IntegrityError:
        # Race condition: another term query inserted the same DOI between our SELECT and INSERT
        if doi:
            cur = db.execute("SELECT id FROM papers WHERE doi = ? AND doi != ''", (doi,))
            row = cur.fetchone()
            if row:
                return row["id"], False
        return None, False


def ensure_stage(db: sqlite3.Connection, paper_id: int, stage: str = "identified") -> None:
    cur = db.execute("SELECT stage FROM stages WHERE paper_id = ?", (paper_id,))
    row = cur.fetchone()
    if row:
        # Don't downgrade: included > eligible > screened > deduplicated > identified
        current_idx = PRISMA_STAGES.index(row["stage"]) if row["stage"] in PRISMA_STAGES else -1
        new_idx = PRISMA_STAGES.index(stage) if stage in PRISMA_STAGES else -1
        if new_idx > current_idx:
            db.execute(
                "UPDATE stages SET stage = ?, updated_at = ? WHERE paper_id = ?",
                (stage, utc_now().isoformat(), paper_id),
            )
    else:
        db.execute(
            "INSERT INTO stages (paper_id, stage, updated_at) VALUES (?, ?, ?)",
            (paper_id, stage, utc_now().isoformat()),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    db = get_db(db_path)

    run_id = utc_now().strftime("%Y%m%d-%H%M%S")
    args_snapshot = {k: v for k, v in vars(args).items() if k != "func"}
    db.execute(
        "INSERT INTO runs (run_id, command, config_snapshot, started_at) VALUES (?, ?, ?, ?)",
        (run_id, "search", json.dumps(args_snapshot, ensure_ascii=False), utc_now().isoformat()),
    )
    db.commit()

    total_inserted = 0
    errors: list[dict[str, str]] = []
    db_counts: dict[str, int] = {}

    for db_name in cfg.databases_enabled:
        searcher = SEARCHERS.get(db_name)
        if not searcher:
            print(f"  [SKIP] Unknown database: {db_name}")
            continue

        print(f"\n{'='*60}")
        print(f"  Database: {db_name}")
        print(f"{'='*60}")

        db_count = 0
        for term in cfg.all_terms:
            if db_count >= cfg.max_per_database:
                print(f"  Reached max_per_database ({cfg.max_per_database}), moving to next database.")
                break

            print(f"  Query: \"{term}\" ...", end=" ", flush=True)
            try:
                papers = searcher(term, cfg)
                before = total_inserted
                for paper in papers:
                    pid, is_new = insert_paper(db, paper)
                    if pid and is_new:
                        ensure_stage(db, pid, "identified")
                        total_inserted += 1
                    elif pid:
                        ensure_stage(db, pid, "identified")
                new = total_inserted - before
                db_count += new
                print(f"{len(papers)} results, {new} new (total DB: {db_count}, overall: {total_inserted})")
            except Exception as exc:
                msg = str(exc)[:200]
                print(f"ERROR: {msg}")
                errors.append({"database": db_name, "term": term, "error": msg})
            # Semantic Scholar needs longer delay (100 req/5min without key)
            sleep_time = 1.2 if db_name == "semantic_scholar" else cfg.sleep
            time.sleep(sleep_time)

        db_counts[db_name] = db_count

    # Update run
    db.execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (utc_now().isoformat(), run_id))
    db.commit()

    # Count identified
    count = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'identified'").fetchone()["n"]

    print(f"\n{'='*60}")
    print(f"  Search complete.")
    print(f"  Papers in database (identified): {count}")
    print(f"  Errors: {len(errors)}")
    print(f"  Database: {db_path}")
    print(f"{'='*60}")

    if errors:
        errors_path = os.path.join(cfg.output_dir, f"search_errors_{run_id}.json")
        write_json(Path(errors_path), errors)
        print(f"  Errors saved to: {errors_path}")

    db.close()
    return 0


def cmd_deduplicate(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    if not Path(db_path).exists():
        print("No review database found. Run 'search' first.")
        return 1
    db = get_db(db_path)

    run_id = utc_now().strftime("%Y%m%d-%H%M%S")
    args_snapshot = {k: v for k, v in vars(args).items() if k != "func"}
    db.execute(
        "INSERT INTO runs (run_id, command, config_snapshot, started_at) VALUES (?, ?, ?, ?)",
        (run_id, "deduplicate", json.dumps(args_snapshot, ensure_ascii=False), utc_now().isoformat()),
    )
    db.commit()

    # Get all papers not yet excluded
    papers = db.execute("""
        SELECT p.id, p.doi, p.title, p.source_database, p.year, p.authors
        FROM papers p
        JOIN stages s ON s.paper_id = p.id
        WHERE s.stage = 'identified'
        ORDER BY p.doi DESC, p.year DESC
    """).fetchall()

    print(f"  Papers to check: {len(papers)}")

    # DOI-based dedup (keep first, mark rest as duplicate)
    doi_groups: dict[str, list[int]] = {}
    for p in papers:
        if p["doi"]:
            doi_groups.setdefault(p["doi"], []).append(p["id"])

    dedup_count = 0
    for doi, ids in doi_groups.items():
        if len(ids) > 1:
            keeper = ids[0]
            for dup_id in ids[1:]:
                db.execute(
                    "INSERT OR IGNORE INTO duplicates (paper_id, duplicate_of_id, match_method, confidence) VALUES (?, ?, ?, ?)",
                    (dup_id, keeper, "doi_exact", 1.0),
                )
                db.execute(
                    "UPDATE stages SET stage = 'deduplicated', exclusion_reason = 'duplicate', "
                    "exclusion_detail = ?, updated_at = ? WHERE paper_id = ?",
                    (f"DOI match: {doi}", utc_now().isoformat(), dup_id),
                )
                dedup_count += 1

    # Title-based fuzzy dedup
    non_dup = db.execute("""
        SELECT p.id, p.title FROM papers p
        JOIN stages s ON s.paper_id = p.id
        WHERE s.stage = 'identified'
        ORDER BY p.year DESC
    """).fetchall()

    fuzzy_count = 0
    threshold = args.fuzzy_threshold
    for i, a in enumerate(non_dup):
        for b in non_dup[i + 1 :]:
            if title_similarity(a["title"], b["title"]) >= threshold:
                db.execute(
                    "INSERT OR IGNORE INTO duplicates (paper_id, duplicate_of_id, match_method, confidence) VALUES (?, ?, ?, ?)",
                    (b["id"], a["id"], "title_fuzzy", round(threshold, 2)),
                )
                db.execute(
                    "UPDATE stages SET stage = 'deduplicated', exclusion_reason = 'duplicate', "
                    "exclusion_detail = ?, updated_at = ? WHERE paper_id = ?",
                    (f"Title similarity with: {a['title'][:100]}", utc_now().isoformat(), b["id"]),
                )
                fuzzy_count += 1

    db.commit()

    remaining = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'identified'").fetchone()["n"]
    total = db.execute("SELECT COUNT(*) as n FROM papers").fetchone()["n"]

    print(f"\n  Deduplication complete.")
    print(f"  DOI exact matches removed:  {dedup_count}")
    print(f"  Title fuzzy matches removed: {fuzzy_count}")
    print(f"  Remaining after dedup:       {remaining}")
    print(f"  Total papers in database:    {total}")

    # Mark non-duplicate identified papers as deduplicated
    db.execute(
        "UPDATE stages SET stage = 'deduplicated', updated_at = ? "
        "WHERE stage = 'identified' AND paper_id NOT IN (SELECT paper_id FROM duplicates)",
        (utc_now().isoformat(),),
    )
    db.commit()

    db.execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (utc_now().isoformat(), run_id))
    db.commit()
    db.close()
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    if not Path(db_path).exists():
        print("No review database found. Run 'search' first.")
        return 1
    db = get_db(db_path)

    papers = db.execute("""
        SELECT p.* FROM papers p
        JOIN stages s ON s.paper_id = p.id
        WHERE s.stage = 'deduplicated'
        ORDER BY p.year DESC, p.source_database
    """).fetchall()

    print(f"  Papers to screen: {len(papers)}")

    if args.output_format == "csv":
        _export_screening_csv(papers, cfg)
    elif args.output_format == "markdown":
        _export_screening_markdown(papers, cfg)
    elif args.output_format == "interactive":
        _screen_interactive(db, papers, cfg)
    else:
        _export_screening_csv(papers, cfg)

    db.close()
    return 0


def _export_screening_csv(papers: list[sqlite3.Row], cfg: Config) -> None:
    # Score each paper by keyword match density for triage
    scored = []
    for p in papers:
        title_l = (p["title"] or "").lower()
        abstract_l = (p["abstract"] or "").lower()
        hit_groups = []
        score = 0
        for group in cfg.keyword_groups:
            group_hit = False
            for term in group["terms"]:
                term_l = term.lower()
                if term_l in title_l:
                    score += 5
                    group_hit = True
                if term_l in abstract_l:
                    score += 2
                    group_hit = True
            if group_hit:
                hit_groups.append(group["label"])
        has_abstract = bool(p["abstract"])
        scored.append((score, hit_groups, has_abstract, p))

    scored.sort(key=lambda x: (x[0], x[3]["year"] or 0), reverse=True)

    out_path = os.path.join(cfg.output_dir, "screening.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "priority_score", "keyword_groups", "has_abstract",
            "year", "title", "authors", "journal", "source_database",
            "abstract", "url", "decision", "notes",
        ])
        for score, hit_groups, has_abstract, p in scored:
            writer.writerow([
                p["id"], score, "; ".join(hit_groups), "Y" if has_abstract else "N",
                p["year"] or "", p["title"], p["authors"] or "", p["journal"] or "",
                p["source_database"], p["abstract"] or "", p["url"] or "", "", "",
            ])
    print(f"  Screening CSV written to: {out_path}")
    print(f"  Columns: id, priority_score, keyword_groups, has_abstract, year, title, authors, journal, source_database, abstract, url, decision, notes")
    print(f"  Papers sorted by priority_score (highest first).")
    print(f"  High-score papers hit more keywords in title+abstract.")
    print(f"  Mark 'include' or 'exclude' in the 'decision' column.")
    print(f"  Then run: python scripts/systematic_review.py --config systematic-review.config.json apply-screening --csv {out_path}")


def _export_screening_markdown(papers: list[sqlite3.Row], cfg: Config) -> None:
    out_path = os.path.join(cfg.output_dir, "screening.md")
    lines = [
        "# Title & Abstract Screening",
        "",
        f"**Total papers to screen:** {len(papers)}",
        "",
        "| # | Year | Title | Authors | Journal | Abstract |",
        "|---|------|-------|---------|---------|----------|",
    ]
    for i, p in enumerate(papers, 1):
        abstract = (p["abstract"] or "")[:300].replace("|", "/").replace("\n", " ")
        title = p["title"].replace("|", "/")
        authors = (p["authors"] or "")[:80].replace("|", "/")
        journal = (p["journal"] or "")[:60].replace("|", "/")
        lines.append(f"| {i} | {p['year'] or ''} | {title} | {authors} | {journal} | {abstract} |")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Screening markdown written to: {out_path}")


def _screen_interactive(db: sqlite3.Connection, papers: list[sqlite3.Row], cfg: Config) -> None:
    print("\n  Interactive screening mode.")
    print("  For each paper, enter: i(nclude), e(xclude), s(kip), q(uit)")
    print(f"  Papers to review: {len(papers)}\n")

    for p in papers:
        print(f"\n  [{p['id']}] {p['year'] or '?'} | {p['source_database']}")
        print(f"  Title: {p['title']}")
        abstract = (p['abstract'] or '')[:500]
        print(f"  Abstract: {abstract}")
        print(f"  Authors: {(p['authors'] or '')[:120]}")

        while True:
            choice = input("  [i/e/s/q]: ").strip().lower()
            if choice == "i":
                db.execute(
                    "UPDATE stages SET stage = 'screened', notes = 'included', updated_at = ? WHERE paper_id = ?",
                    (utc_now().isoformat(), p["id"]),
                )
                db.commit()
                break
            elif choice == "e":
                reason = input("  Exclusion reason (wrong_topic/wrong_population/not_empirical/other): ").strip()
                db.execute(
                    "UPDATE stages SET stage = 'screened', exclusion_reason = ?, notes = 'excluded', updated_at = ? WHERE paper_id = ?",
                    (reason or "other", utc_now().isoformat(), p["id"]),
                )
                db.commit()
                break
            elif choice == "s":
                break
            elif choice == "q":
                print("  Quitting screening. Progress saved.")
                return
    print("\n  Screening complete.")


def cmd_apply_screening(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    db = get_db(db_path)

    csv_path = args.csv
    if not os.path.exists(csv_path):
        print(f"CSV file not found: {csv_path}")
        return 1

    included = 0
    excluded = 0
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            paper_id = int(row.get("id", 0))
            decision = (row.get("decision") or "").strip().lower()
            notes = (row.get("notes") or "").strip()
            if not paper_id or not decision:
                continue
            if decision.startswith("i"):
                db.execute(
                    "UPDATE stages SET stage = 'screened', notes = ?, updated_at = ? WHERE paper_id = ?",
                    (notes or "included", utc_now().isoformat(), paper_id),
                )
                included += 1
            elif decision.startswith("e"):
                reason = notes or "other"
                db.execute(
                    "UPDATE stages SET stage = 'screened', exclusion_reason = ?, notes = ?, updated_at = ? WHERE paper_id = ?",
                    (reason, "excluded", utc_now().isoformat(), paper_id),
                )
                excluded += 1
    db.commit()

    remaining = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'deduplicated'").fetchone()["n"]
    print(f"  Screening decisions applied: {included} included, {excluded} excluded, {remaining} remaining unscreened")
    db.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    if not Path(db_path).exists():
        print("No review database found. Run 'search' first.")
        return 1
    db = get_db(db_path)

    total = db.execute("SELECT COUNT(*) as n FROM papers").fetchone()["n"]
    identified = total
    duplicates = db.execute("SELECT COUNT(*) as n FROM duplicates").fetchone()["n"]
    deduplicated = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'deduplicated'").fetchone()["n"]
    screened_in = db.execute(
        "SELECT COUNT(*) as n FROM stages WHERE stage = 'screened' AND (notes = 'included' OR exclusion_reason = '')"
    ).fetchone()["n"]
    screened_out = db.execute(
        "SELECT COUNT(*) as n FROM stages WHERE stage = 'screened' AND exclusion_reason != '' AND exclusion_reason != 'duplicate'"
    ).fetchone()["n"]
    eligible = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'eligible'").fetchone()["n"]
    included = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = 'included'").fetchone()["n"]

    # Exclusion reasons breakdown
    reasons = db.execute("""
        SELECT exclusion_reason, COUNT(*) as n FROM stages
        WHERE exclusion_reason != '' AND exclusion_reason != 'duplicate'
        GROUP BY exclusion_reason ORDER BY n DESC
    """).fetchall()

    # By database
    by_db = db.execute("""
        SELECT source_database, COUNT(*) as n FROM papers
        GROUP BY source_database ORDER BY n DESC
    """).fetchall()

    # By year
    by_year = db.execute("""
        SELECT year, COUNT(*) as n FROM papers
        WHERE year IS NOT NULL
        GROUP BY year ORDER BY year DESC
    """).fetchall()

    print("\n" + "=" * 70)
    print("  PRISMA 2020 FLOW DIAGRAM DATA")
    print("=" * 70)
    print(f"""
  ┌─────────────────────────────────────────────────┐
  │ Records identified from databases:    {identified:>6}       │
  │   {"Semantic Scholar":<30} {_db_count(by_db, 'semantic_scholar'):>6}       │
  │   {"OpenAlex":<30} {_db_count(by_db, 'openalex'):>6}       │
  │   {"ERIC":<30} {_db_count(by_db, 'eric'):>6}       │
  │   {"Crossref":<30} {_db_count(by_db, 'crossref'):>6}       │
  │   {"arXiv":<30} {_db_count(by_db, 'arxiv'):>6}       │
  └───────────────────┬─────────────────────────────┘
                      │
                      ▼
  ┌─────────────────────────────────────────────────┐
  │ Records after duplicates removed:    {deduplicated + (total - identified - duplicates):>6}       │
  │   Duplicates removed:                {duplicates:>6}       │
  └───────────────────┬─────────────────────────────┘
                      │
                      ▼
  ┌─────────────────────────────────────────────────┐
  │ Records screened (title/abstract):   {deduplicated:>6}       │
  └───────────────────┬─────────────────────────────┘
                      │
                      ▼
  ┌─────────────────────────────────────────────────┐
  │ Records included after screening:    {screened_in:>6}       │
  │ Records excluded:                    {screened_out:>6}       │
  └───────────────────┬─────────────────────────────┘
                      │
                      ▼
  ┌─────────────────────────────────────────────────┐
  │ Full-text assessed for eligibility:  {screened_in:>6}       │
  └───────────────────┬─────────────────────────────┘
                      │
                      ▼
  ┌─────────────────────────────────────────────────┐
  │ Studies included in review:          {included:>6}       │
  └─────────────────────────────────────────────────┘
""")

    if reasons:
        print("  Exclusion reasons:")
        for r in reasons:
            print(f"    - {r['exclusion_reason']}: {r['n']}")

    print(f"\n  By year:")
    for y in by_year[:10]:
        print(f"    {y['year']}: {y['n']}")

    # Export full PRISMA data as JSON
    prisma_data = {
        "generated_at": utc_now().isoformat(),
        "stages": {
            "identified": identified,
            "duplicates_removed": duplicates,
            "after_deduplication": deduplicated + (total - identified - duplicates),
            "screened": deduplicated,
            "included_after_screening": screened_in,
            "excluded_after_screening": screened_out,
            "full_text_assessed": screened_in,
            "included_in_review": included,
        },
        "exclusion_reasons": [{"reason": r["exclusion_reason"], "count": r["n"]} for r in reasons],
        "by_database": [{"database": r["source_database"], "count": r["n"]} for r in by_db],
        "by_year": [{"year": r["year"], "count": r["n"]} for r in by_year],
    }
    prisma_path = os.path.join(cfg.output_dir, "prisma_flow.json")
    write_json(Path(prisma_path), prisma_data)
    print(f"\n  PRISMA data exported to: {prisma_path}")

    db.close()
    return 0


def _db_count(rows: list[sqlite3.Row], name: str) -> int:
    for r in rows:
        if r["source_database"] == name:
            return r["n"]
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    if not Path(db_path).exists():
        print("No review database found. Run 'search' first.")
        return 1
    db = get_db(db_path)

    stage_filter = args.stage or "screened"
    papers = db.execute("""
        SELECT p.*, s.stage, s.exclusion_reason, s.notes as screening_notes
        FROM papers p
        JOIN stages s ON s.paper_id = p.id
        WHERE s.stage = ? AND (s.exclusion_reason = '' OR s.exclusion_reason = 'duplicate' OR s.exclusion_reason IS NULL)
        ORDER BY p.year DESC, p.title
    """, (stage_filter,)).fetchall()

    if args.format == "csv":
        out_path = os.path.join(cfg.output_dir, f"papers_{stage_filter}.csv")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["title", "authors", "year", "journal", "doi", "abstract", "url", "source_database", "stage"])
            for p in papers:
                writer.writerow([p["title"], p["authors"], p["year"], p["journal"], p["doi"], p["abstract"], p["url"], p["source_database"], p["stage"]])
        print(f"  Exported {len(papers)} papers to: {out_path}")

    elif args.format == "bibtex":
        out_path = os.path.join(cfg.output_dir, f"papers_{stage_filter}.bib")
        with open(out_path, "w", encoding="utf-8") as f:
            for p in papers:
                key = f"{p['authors'][:20].replace(' ', '').replace(';','') if p['authors'] else 'unknown'}{p['year'] or '0000'}{p['title'][:30].replace(' ', '')}"
                key = re.sub(r"[^a-zA-Z0-9]", "", key)
                f.write(f"@article{{{key},\n")
                f.write(f"  title = {{{{{p['title']}}}}},\n")
                f.write(f"  author = {{{{{p['authors']}}}}},\n" if p["authors"] else "")
                f.write(f"  year = {{{{{p['year']}}}}},\n" if p["year"] else "")
                f.write(f"  journal = {{{{{p['journal']}}}}},\n" if p["journal"] else "")
                f.write(f"  doi = {{{{{p['doi']}}}}},\n" if p["doi"] else "")
                f.write(f"  url = {{{{{p['url']}}}}},\n" if p["url"] else "")
                f.write(f"  abstract = {{{{{p['abstract'][:500]}}}}},\n" if p["abstract"] else "")
                f.write("}\n\n")
        print(f"  Exported {len(papers)} papers to: {out_path}")

    elif args.format == "json":
        out_path = os.path.join(cfg.output_dir, f"papers_{stage_filter}.json")
        papers_list = [dict(p) for p in papers]
        write_json(Path(out_path), {"count": len(papers_list), "papers": papers_list})
        print(f"  Exported {len(papers_list)} papers to: {out_path}")

    db.close()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = Config(args.config)
    db_path = os.path.join(cfg.output_dir, "review.db")
    if args.stage:
        db = get_db(db_path)
        db.execute("UPDATE stages SET stage = ?, exclusion_reason = '', notes = '', updated_at = ? WHERE stage = ?",
                   (args.stage, utc_now().isoformat(), "identified"))
        db.commit()
        count = db.execute("SELECT COUNT(*) as n FROM stages WHERE stage = ?", (args.stage,)).fetchone()["n"]
        print(f"  Reset {count} papers to stage: {args.stage}")
        db.close()
    elif args.hard:
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"  Removed database: {db_path}")
        # Also remove data files
        import glob as _glob
        for pattern in ["search_errors_*.json", "screening.*", "papers_*.csv", "papers_*.bib", "papers_*.json", "prisma_flow.json"]:
            for f in _glob.glob(os.path.join(cfg.output_dir, pattern)):
                os.remove(f)
                print(f"  Removed: {f}")
    else:
        print("Use --stage <stage> to move all papers back to a stage, or --hard to delete everything.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Systematic literature review pipeline with PRISMA tracking."
    )
    parser.add_argument("--config", default="systematic-review.config.json", help="Path to config JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    sp_search = subparsers.add_parser("search", help="Search all configured databases.")
    sp_search.set_defaults(func=cmd_search)

    # deduplicate
    sp_dedup = subparsers.add_parser("deduplicate", help="Deduplicate papers by DOI and title similarity.")
    sp_dedup.add_argument("--fuzzy-threshold", type=float, default=0.85, help="Title similarity threshold (0.0-1.0)")
    sp_dedup.set_defaults(func=cmd_deduplicate)

    # screen
    sp_screen = subparsers.add_parser("screen", help="Generate screening output or run interactive screening.")
    sp_screen.add_argument("--format", dest="output_format", choices=["csv", "markdown", "interactive"], default="csv",
                           help="Screening output format (default: csv)")
    sp_screen.set_defaults(func=cmd_screen)

    # apply-screening
    sp_apply = subparsers.add_parser("apply-screening", help="Apply screening decisions from a CSV file.")
    sp_apply.add_argument("--csv", required=True, help="Path to screening CSV with decisions filled in.")
    sp_apply.set_defaults(func=cmd_apply_screening)

    # report
    sp_report = subparsers.add_parser("report", help="Generate PRISMA flowchart data.")
    sp_report.set_defaults(func=cmd_report)

    # export
    sp_export = subparsers.add_parser("export", help="Export papers at a given PRISMA stage.")
    sp_export.add_argument("--stage", default="screened", choices=PRISMA_STAGES,
                           help="PRISMA stage to export (default: screened)")
    sp_export.add_argument("--format", default="csv", choices=["csv", "bibtex", "json"],
                           help="Export format (default: csv)")
    sp_export.set_defaults(func=cmd_export)

    # reset
    sp_reset = subparsers.add_parser("reset", help="Reset database state.")
    sp_reset.add_argument("--stage", choices=PRISMA_STAGES, help="Move all papers back to this stage.")
    sp_reset.add_argument("--hard", action="store_true", help="Delete entire database and all output files.")
    sp_reset.set_defaults(func=cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
