# Local output directory

Crawl results live in `data/iranfoia/` by default and are excluded from Git. PDFs stay on the local workstation; GitHub stores only crawler source, tests and documentation.

Expected generated layout:

```text
data/iranfoia/
  pdfs/
  manifest.csv
  summary.json
  discovered.jsonl
  errors.jsonl          (when errors occur)
  duplicates.jsonl      (when duplicates occur)
  skipped.jsonl         (when links are skipped)
  debug/
```

Use `--out data/probe` for a separate short diagnostic run. Old `iranfoia_probe/` output is retained unless you remove it yourself. Never commit collected documents without verifying their reuse permissions and privacy constraints.
