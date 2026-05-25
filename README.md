# Systematic Review Tool

A zero-dependency Python pipeline for PRISMA-compliant systematic literature reviews. Search multiple academic databases, deduplicate results, screen papers, and generate PRISMA flow diagrams — all from the command line.

## Quick Start

```bash
# 1. Copy and edit the config
cp config.template.json review.config.json
# Fill in your keyword groups, email, and date range

# 2. Search across databases
python3 systematic_review.py --config review.config.json search

# 3. Deduplicate
python3 systematic_review.py --config review.config.json deduplicate

# 4. Generate screening CSV (open in Excel/Numbers, mark include/exclude)
python3 systematic_review.py --config review.config.json screen --format csv

# 5. Apply your screening decisions
python3 systematic_review.py --config review.config.json apply-screening --csv review-output/screening.csv

# 6. View PRISMA flow diagram
python3 systematic_review.py --config review.config.json report

# 7. Export final results
python3 systematic_review.py --config review.config.json export --stage screened --format bibtex
```

## Requirements

Python 3.9+. No pip install needed — uses only standard library modules.

## Databases

| Database | Coverage | API Key |
|---|---|---|
| OpenAlex | All disciplines, 250M+ works | None required |
| ERIC | Education sciences | None required |
| Crossref | Journal articles, DOIs | None required (email recommended) |
| Semantic Scholar | All disciplines, citation graph | Free key recommended |
| arXiv | Preprints (physics, CS, math) | None required |

## PRISMA Stages

```
identified → deduplicated → screened → eligible → included
```

Every paper's progression through these stages is tracked in a SQLite database, providing a full audit trail for your methodology chapter.

## Commands

| Command | Description |
|---|---|
| `search` | Query all enabled databases with your keyword groups |
| `deduplicate` | Remove duplicates by DOI and title similarity |
| `screen` | Generate CSV/markdown for title/abstract screening, or interactive mode |
| `apply-screening` | Apply screening decisions from a filled CSV back to the database |
| `report` | Print PRISMA 2020 flow diagram data and export as JSON |
| `export` | Export papers at any PRISMA stage to CSV, BibTeX, or JSON |
| `reset` | Reset database state (`--hard` to delete everything, `--stage` to rewind) |

## Config Reference

```json
{
  "databases": {},          // Enable/disable data sources
  "keyword_groups": [],     // Grouped search terms with labels
  "date_from": "2015-01-01", // Start of search window
  "date_until": "2026-05-25", // End of search window
  "max_per_query": 100,     // Results per keyword per API call
  "max_per_database": 1500, // Stop searching a database after N unique papers
  "sleep": 0.5,            // Delay between API calls (seconds)
  "crossref_mailto": "",    // Polite pool email for Crossref
  "output_dir": "review-output", // Where to store database and exports
  "language": "en"          // Language preference
}
```

## Tips

- **Semantic Scholar** requires a free API key for bulk use. Get one at [api.semanticscholar.org](https://api.semanticscholar.org/) and the script will use it.
- The screening CSV is sorted by `priority_score` — papers with more keyword matches in title/abstract rank higher. Batch-exclude score=0 papers to save time.
- Run `search` periodically to pick up newly published papers without losing your existing screening decisions.
- For PhD methodology: the `report` command exports `prisma_flow.json` with exact counts for each PRISMA stage, and every database search is logged with timestamps.

## License

MIT
