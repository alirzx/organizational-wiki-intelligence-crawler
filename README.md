# Wiki Hami | IranFOIA Public PDF Crawler

A local-only Python/Playwright crawler that collects publicly available PDF documents from IranFOIA for **Wiki Hami's document scanner, sectioner, and linker** evaluation. It does not require Docker, Compose, a service, a remote database, or a GPU.

## Scope

Default start: `https://iranfoia.ir/document/orgs/`. The crawler discovers relevant links on that page and follows eligible document pages, including `https://iranfoia.ir/document/list/` when linked. It supports HTML links, browser-rendered JavaScript pagination, downloadable controls, and attachment URLs found in public JSON responses already loaded by the browser.

**Only validated PDF files are saved.** A download URL ending in `.jpg` may serve a PDF; the crawler verifies the PDF header **and parses page structure using pypdf**. Actual JPG/PNG, DOCX, ZIP, HTML error pages, and corrupt PDFs are not accepted.

The crawler does not guarantee access to all documents on the site. Number of files discovered depends on live page structure, public access, and crawl limits. HTTP 401/403 and robots.txt restrictions are respected; no login, CAPTCHA, or authorization bypass.

## Install (Ubuntu)

```bash
cd /mnt/X/hami_system_sharif/wiki_crawler
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Use local Chromium, since Playwright CDN may reject downloads.
command -v chromium
chromium --version
```

If Chromium is not installed, use your normal system package manager. The tested Ubuntu setup used `sudo snap install chromium`; no `python -m playwright install chromium` is needed with `--browser-executable`.

## Small probe

```bash
source .venv/bin/activate
python crawl.py \
  --start https://iranfoia.ir/document/orgs/ \
  --mode browser \
  --browser-executable "$(command -v chromium)" \
  --headful \
  --out data/probe \
  --max-files 5 --max-pages 12 --max-ui-pages 8 \
  --delay 2 --browser-wait-ms 2500 \
  --save-html --verbose
```

## Larger local collection

```bash
python crawl.py \
  --start https://iranfoia.ir/document/orgs/ \
  --mode browser \
  --browser-executable "$(command -v chromium)" \
  --scope documents \
  --out data/iranfoia \
  --max-files 200 --max-pages 400 --max-ui-pages 100 \
  --delay 2.5 --browser-wait-ms 2500 \
  --max-mb 35 --save-html --verbose
```

If document-list navigation is the only desired entry point, use `--start https://iranfoia.ir/document/list/`. Use `--scope same-host` only for a carefully bounded diagnostic crawl because it follows many more unrelated pages.

`--max-pages` now limits **rendered pages**, including JavaScript pagination states, in browser mode. `--max-ui-pages` limits pagination steps per visited URL. `--max-files` limits **the total unique saved PDF count**, including files restored from an existing manifest. The crawler processes downloads on each rendered page before clicking Next.

Browser mode supports JavaScript; static mode is a requests-only fallback and may miss JS pagination. Neither mode bypasses access controls.

## Local output (Git ignored)

```text
data/iranfoia/
  pdfs/                   # Unique, structurally validated .pdf files
  manifest.csv            # PDF path, SHA256, pages, download/source URLs
  summary.json            # Unique URLs, rendered pages, candidates, saved files
  discovered.jsonl        # Candidate links
  errors.jsonl            # Failed requests/invalid PDFs, when present
  duplicates.jsonl        # Duplicate PDF bodies, when present
  skipped.jsonl           # Out-of-scope links, when present
  debug/                  # HTML captures and network JSON URL diagnostics
```

Files are named using a content-hash prefix to avoid collisions. `manifest.csv` is saved atomically. On rerun, existing files are counted toward the limit only when their file size and SHA-256 match the manifest; do not move the `pdfs/` folder without its manifest. A rerun starts page discovery from the beginning, but does not re-add matching PDFs. The crawler does not yet persist a URL pagination cursor.

Previously downloaded `iranfoia_probe/` files are unchanged. A new run with `--out data/iranfoia` creates a separate dataset.

## Inspect results

```bash
cat data/iranfoia/summary.json
find data/iranfoia/pdfs -type f -name '*.pdf' | wc -l
du -sh data/iranfoia/pdfs
head -n 4 data/iranfoia/manifest.csv
[ ! -f data/iranfoia/errors.jsonl ] || tail -n 20 data/iranfoia/errors.jsonl
```

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m py_compile crawl.py
```

Automated tests use local mock pages, not live IranFOIA. The initial Ubuntu probe reported **5 public PDF downloads**, two 401 responses, and JavaScript navigation on document/list pages. Those observations are not a comprehensive inventory of IranFOIA's HTML, internal APIs, or file count.

**Data governance:** Retain source URLs and hashes, obey website rules, and review license and personal information before redistributing downloaded documents. For Wiki Hami OCR evaluation, add separate human-reviewed labels for scanned, printed, handwritten, tables, stamps, and diagrams. Blank organizational forms are not handwriting ground truth.
