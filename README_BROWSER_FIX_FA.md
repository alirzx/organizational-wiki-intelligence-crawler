# رفع مشکل دانلود Playwright Chromium (HTTP 403)

پکیج‌های Python نصب شده‌اند؛ خطای 403 مربوط به دانلود مرورگر از CDN است. این نسخه از `crawl.py` گزینه `--browser-executable` و متغیر محیطی `WIKI_CRAWLER_BROWSER_PATH` را اضافه می‌کند تا مرورگر Chrome/Chromium از قبل نصب‌شده روی Ubuntu استفاده شود.

## جایگزینی فایل بدون تغییر باقی پروژه

```bash
cd /mnt/X/hami_system_sharif/wiki_crawler
cp crawl.py crawl.py.bak
# فایل crawl.py موجود در این بسته را جایگزین فایل فعلی کنید.
source .venv/bin/activate
python -m py_compile crawl.py
python -m pytest -q
```

## تشخیص مرورگر

```bash
BROWSER="$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || command -v chromium-browser)"
printf 'browser: %s\n' "$BROWSER"
```

اگر مسیر خالی بود و از snap استفاده می‌کنید:

```bash
sudo snap install chromium
BROWSER="$(command -v chromium || command -v chromium-browser)"
```

اگر دستور snap قابل استفاده نیست، Chrome یا Chromium را از مخازن معتبر سیستم/وب‌سایت رسمی نصب کنید و مسیر فایل اجرایی را به `BROWSER` اختصاص دهید.

## تشخیص سلامت مرورگر

```bash
python - "$BROWSER" <<'PY'
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=sys.argv[1])
    print('Playwright browser:', b.version)
    b.close()
PY
```

## اجرای محدود crawler

```bash
python crawl.py --mode browser --browser-executable "$BROWSER" \
  --headful --out ./iranfoia_probe --max-files 5 --max-pages 8 \
  --max-ui-pages 8 --delay 2 --browser-wait-ms 2500 \
  --save-html --verbose
```

نتایج `iranfoia_probe/summary.json`, `manifest.csv`, `errors.jsonl`, `discovered.jsonl` را بررسی کنید. تعداد فایل واقعی سایت هنوز مشخص نیست؛ `--max-files` فقط سقف است.
