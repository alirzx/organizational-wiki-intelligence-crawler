#!/usr/bin/env python3
"""Public-document crawler for https://iranfoia.ir/document/orgs/.

Uses public website navigation, not undocumented/authenticated APIs. Discovers
HTML, pagination, attachments and browser downloads; verifies PDF signatures.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger('iranfoia')
DEFAULT_START = 'https://iranfoia.ir/document/orgs/'
UA = 'WikiHamiPublicDocumentResearchBot/1.0 (respectful public PDF corpus collection)'
PDF_RE = re.compile(r'(?i)\.pdf(?:$|[?#])')
FILE_RE = re.compile(r'(?i)(?:\.pdf(?:$|[?#])|/(?:download|attachment|attachments|file|files|media|getfile|filedownload)(?:/|\?|$)|[?&](?:download|file_id|attachment_id|fileid|pdf)=)')
DOC_RE = re.compile(r'(?i)(?:/document(?:s)?(?:/|$)|/orgs(?:/|$)|/published(?:/|$)|/attachments?(?:/|$)|[?&](?:page|p|pageNumber|org|organization|document|id)=)')
PDF_WORDS = ('دانلود', 'دریافت', 'پیوست', 'فایل', 'pdf', 'download', 'attachment', 'document file', 'بارگیری')
DOWNLOAD_BUTTON_WORDS = ('دانلود', 'دریافت فایل', 'فایل پیوست', 'مشاهده فایل', 'مشاهده سند', 'download', 'pdf', 'بارگیری')
PAGINATION_WORDS = {'next', 'بعدی', 'بعد', 'صفحه بعد', 'صفحه بعدی', '»', '›', '→'}
DANGEROUS_WORDS = ('ثبت درخواست', 'ارسال', 'حذف', 'ورود', 'خروج', 'ثبت نام', 'پرداخت', 'submit', 'delete', 'login', 'logout', 'payment')
FIELDS = ('file', 'sha256', 'size_bytes', 'pages', 'mime_type', 'download_url', 'source_page', 'source_title', 'anchor_text', 'found_via', 'downloaded_utc')


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize(url: str, base: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    s = url.strip().strip('"\'')
    if not s or s.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:', 'blob:')):
        return None
    absolute, _ = urldefrag(urljoin(base, s))
    p = urlparse(absolute)
    if p.scheme not in ('http', 'https') or not p.hostname:
        return None
    # Remove tracking keys, preserving pagination/filter parameters.
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path or '/', p.params,
                       urlencode(query, doseq=True), ''))


def allowed_host(url: str, hosts: set[str]) -> bool:
    return (urlparse(url).hostname or '').lower() in hosts


def kind_for(url: str, context: str = '') -> str:
    if PDF_RE.search(url):
        return 'file'
    if FILE_RE.search(url) or re.search(r'(?i)[?&](?:download|export|file)=', url):
        return 'file'
    if re.search(r'(?i)\.((?:jpg|jpeg|png|webp|zip|rar|xlsx?|docx?))(?:[?#]|$)', url):
        return 'skip'
    if re.search(r'(?i)\.(?:css|js|map|woff2?|ttf|svg|ico)(?:[?#]|$)', url):
        return 'skip'
    if DOC_RE.search(url):
        return 'page'
    if any(word in context.lower() for word in PDF_WORDS):
        return 'file_or_page'
    return 'unknown'


def extract_links(html: str, base: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    keys = ('href', 'data-href', 'data-url', 'data-download', 'data-file', 'data-pdf', 'src')
    for tag in soup.find_all(['a', 'button', 'iframe', 'embed', 'object', 'source']):
        text = ' '.join(tag.stripped_strings)[:180]
        if not text:
            text = ' '.join(str(tag.get(k, '')) for k in ('title', 'aria-label', 'download'))[:180]
        values = [str(tag.get(k)) for k in keys if tag.get(k)]
        # Recover literal URLs embedded in simple onclick="window.open('/file/1')".
        onclick = str(tag.get('onclick') or '')
        if onclick and any(w in onclick.lower() for w in ('open(', 'location', 'download', 'pdf', 'file')):
            values += re.findall(r"['\"]([^'\"]{2,350})['\"]", onclick)
        for raw in values:
            link = normalize(raw, base)
            if link:
                results.append({'url': link, 'text': text, 'tag': tag.name})
    # PDF links embedded as plain strings in an HTML page or serialized state.
    for raw in re.findall(r'''(?:https?://[^\s'"<>\\]+?\.pdf(?:\?[^\s'"<>\\]*)?|/[^\s'"<>\\]+?\.pdf(?:\?[^\s'"<>\\]*)?)''', html, re.I):
        link = normalize(raw.replace('\\/', '/').replace('&amp;', '&'), base)
        if link:
            results.append({'url': link, 'text': '', 'tag': 'embedded'})
    return results


def extract_json_links(value: Any, base: str, limit: int = 1000) -> list[str]:
    """Find URL-shaped PDF/attachment strings in publicly returned JSON."""
    found: set[str] = set()
    pending = [value]
    steps = 0
    while pending and steps < limit:
        steps += 1
        obj = pending.pop()
        if isinstance(obj, dict):
            pending.extend(list(obj.values())[:200])
        elif isinstance(obj, list):
            pending.extend(obj[:200])
        elif isinstance(obj, str):
            url = normalize(obj, base)
            if url and kind_for(url) == 'file':
                found.add(url)
    return sorted(found)


def pdf_ok(data: bytes) -> bool:
    # PDF header is allowed after a short binary prefix, e.g. a BOM.
    return b'%PDF-' in data[:1024] and len(data) >= 16


def safe_name(s: str) -> str:
    s = unquote(s or '')
    s = s.rsplit('/', 1)[-1].split('?')[0]
    s = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', s).strip(' .')
    s = re.sub(r'\s+', '_', s)
    if not s or not s.lower().endswith('.pdf'):
        s = (s or 'document') + '.pdf'
    return s[-100:]


def title_for(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for selector in ('h1', 'h2', 'title'):
        el = soup.select_one(selector)
        if el and el.get_text(' ', strip=True):
            return el.get_text(' ', strip=True)[:180]
    return ''


@dataclass
class Candidate:
    url: str
    source_page: str
    source_title: str = ''
    anchor_text: str = ''
    found_via: str = 'html'


class Crawler:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.base = normalize(args.start, args.start)
        if not self.base:
            raise ValueError('Invalid start URL')
        host = urlparse(self.base).hostname.lower()
        self.hosts = {host}
        # Consider www and apex of the supplied host the same first-party site.
        self.hosts.add(host[4:] if host.startswith('www.') else 'www.' + host)
        self.hosts.update(x.lower() for x in args.allow_host)
        self.out = Path(args.out)
        self.pdf_dir = self.out / 'pdfs'
        self.debug_dir = self.out / 'debug'
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/pdf,*/*'})
        # Retry transient 429/5xx but never an authentication/authorization error.
        retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504), respect_retry_after_header=True)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.session.mount('http://', HTTPAdapter(max_retries=retry))
        self.robots: dict[str, RobotFileParser | bool] = {}
        self.visited: set[str] = set()
        self.pending_pages: deque[str] = deque([self.base])
        self.pending_files: deque[Candidate] = deque()
        self.files_seen: set[str] = set()
        self.hashes: set[str] = set()
        self.entries: list[dict[str, Any]] = []
        self.ui_pages_scanned = 0
        self.initial_file_count = 0
        self.pdf_candidates_seen = 0
        self.download_failures = 0
        self.duplicate_count = 0
        self.last_request_time = 0.0
        self.last_network_activity: list[dict[str, str]] = []
        self.browser_downloads = []
        self.browser_pdf_responses = []
        self._load_manifest()

    def _load_manifest(self):
        manifest = self.out / 'manifest.csv'
        if not manifest.exists():
            return
        with manifest.open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                if not row.get('file', '').startswith('pdfs/'):
                    continue
                existing = self.out / row['file']
                if existing.is_file() and existing.stat().st_size == int(row.get('size_bytes') or -1):
                    # Verify the checksum on resume, not only the filename.
                    if hashlib.sha256(existing.read_bytes()).hexdigest() != row.get('sha256', ''):
                        LOG.warning('Ignoring modified PDF on resume: %s', existing)
                        continue
                    self.entries.append(row)
                    self.files_seen.add(row.get('download_url', ''))
                    self.hashes.add(row.get('sha256', ''))
        self.initial_file_count = len(self.entries)
        LOG.info('Resuming: %s existing validated-file records', len(self.entries))

    def _log(self, filename: str, item: dict):
        with (self.out / filename).open('a', encoding='utf-8') as f:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

    def _save_manifest(self):
        tmp_manifest = self.out / 'manifest.csv.tmp'
        with tmp_manifest.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.entries)
        tmp_manifest.replace(self.out / 'manifest.csv')

    def _throttle(self):
        wait = self.args.delay - (time.monotonic() - self.last_request_time)
        if wait > 0:
            time.sleep(wait)
        self.last_request_time = time.monotonic()

    def allowed_by_robots(self, url: str) -> bool:
        p = urlparse(url)
        root = f'{p.scheme}://{p.netloc}'
        if root not in self.robots:
            robots_url = root + '/robots.txt'
            try:
                self._throttle()
                resp = self.session.get(robots_url, timeout=self.args.timeout, allow_redirects=False)
                if resp.status_code == 404 or resp.status_code == 410:
                    self.robots[root] = True
                elif resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    self.robots[root] = parser
                    crawl_delay = parser.crawl_delay(UA) or parser.crawl_delay('*')
                    if crawl_delay:
                        self.args.delay = max(self.args.delay, float(crawl_delay))
                else:
                    LOG.warning('robots.txt %s returned HTTP %s; skip host (fail closed)', robots_url, resp.status_code)
                    self.robots[root] = False
            except requests.RequestException as exc:
                LOG.warning('robots.txt inaccessible (%s); skip host (fail closed)', exc)
                self.robots[root] = False
        rule = self.robots[root]
        return rule if isinstance(rule, bool) else rule.can_fetch(UA, url)

    def queue(self, url: str, source: str, title: str, text: str, via: str = 'html'):
        if not allowed_host(url, self.hosts):
            self._log('skipped.jsonl', {'url': url, 'why': 'outside allowed hosts', 'source': source})
            return
        kind = kind_for(url, text)
        if kind == 'file' or kind == 'file_or_page':
            if url not in self.files_seen:
                self.files_seen.add(url)
                self.pending_files.append(Candidate(url, source, title, text, via))
                self.pdf_candidates_seen += 1
                self._log('discovered.jsonl', {'url': url, 'source_page': source, 'context': text, 'kind': kind})
        elif kind == 'page' or self.args.scope == 'same-host':
            if url not in self.visited and url not in self.pending_pages:
                self.pending_pages.append(url)

    def feed_html(self, html: str, url: str, label: str | None = None):
        title = title_for(html)
        links = extract_links(html, url)
        LOG.info('Page %s : title=%r, links=%d', label or url, title[:70], len(links))
        for link in links:
            self.queue(link['url'], url, title, link['text'])
        return links

    def _direct_get(self, url: str, *, stream: bool = False) -> requests.Response:
        current = url
        for _ in range(5):
            if not allowed_host(current, self.hosts) or not self.allowed_by_robots(current):
                raise RuntimeError(f'Host/path not authorized by whitelist and robots.txt: {current}')
            self._throttle()
            response = self.session.get(current, stream=stream, timeout=(self.args.timeout, self.args.timeout), allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                nxt = normalize(response.headers.get('Location', ''), current)
                response.close()
                if not nxt:
                    raise RuntimeError(f'Invalid redirect from {current}')
                current = nxt
                continue
            if response.status_code in (401, 403, 429):
                raise RuntimeError(f'Access restriction HTTP {response.status_code} on {current}; stop, no bypass')
            response.raise_for_status()
            return response
        raise RuntimeError('Too many redirects')

    @staticmethod
    def _validated_pdf_page_count(data: bytes) -> int:
        """Accept only PDFs that are structurally readable, not just PDF-looking bytes."""
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            count = len(reader.pages)
            if count < 1:
                raise ValueError('Zero-page PDF')
            return count
        except Exception as exc:
            raise ValueError(f'Unreadable/invalid PDF: {exc}') from exc

    def _store(self, data: bytes, item: Candidate, mime: str, name: str = '') -> bool:
        if not pdf_ok(data):
            raise ValueError(f'Not a valid PDF (%s bytes, mime=%s)' % (len(data), mime))
        pages = self._validated_pdf_page_count(data)
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.hashes:
            self.duplicate_count += 1
            self._log('duplicates.jsonl', {'download_url': item.url, 'sha256': digest, 'source': item.source_page})
            return False
        fname = digest[:14] + '_' + safe_name(name or urlparse(item.url).path)
        target = self.pdf_dir / fname
        tmp = target.with_name(target.name + '.part')
        tmp.write_bytes(data)
        tmp.replace(target)
        row = {'file': str(target.relative_to(self.out)), 'sha256': digest,
               'size_bytes': len(data), 'pages': pages, 'mime_type': mime,
               'download_url': item.url, 'source_page': item.source_page,
               'source_title': item.source_title, 'anchor_text': item.anchor_text,
               'found_via': item.found_via, 'downloaded_utc': now()}
        self.entries.append(row)
        self.hashes.add(digest)
        self._save_manifest()
        LOG.info('SAVED [%s/%s] %s (%s bytes)', len(self.entries), self.args.max_files, fname, len(data))
        return True

    def _save_download(self, item: Candidate):
        response = self._direct_get(item.url, stream=True)
        try:
            mime = response.headers.get('Content-Type', '').split(';')[0].lower()
            length = int(response.headers.get('Content-Length') or 0)
            if length > self.args.max_mb * 1024**2:
                raise ValueError('PDF larger than configured max size')
            data = bytearray()
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if chunk:
                    data.extend(chunk)
                    if len(data) > self.args.max_mb * 1024**2:
                        raise ValueError('File exceeds max size')
            name = ''
            cd = response.headers.get('Content-Disposition', '')
            m = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", cd, re.I)
            if m:
                name = m.group(1)
            if mime.startswith('text/html') and not PDF_RE.search(response.url):
                # A document-looking link may lead to an HTML detail page, not a file.
                if response.url not in self.visited and response.url not in self.pending_pages:
                    self.pending_pages.append(response.url)
                self._log('skipped.jsonl', {'url': item.url, 'why': 'HTML detail page, queued for scanning'})
                return
            self._store(bytes(data), item, mime, name)
        finally:
            response.close()

    def _capture_browser(self, page):
        # Attach once per new browser page; callbacks only collect metadata.
        if getattr(page, '_wiki_capture_installed', False):
            return
        page._wiki_capture_installed = True

        def on_response(resp):
            try:
                hdrs = resp.headers
                mime = hdrs.get('content-type', '').lower()
                url = resp.url
                if 'application/pdf' in mime or PDF_RE.search(url):
                    self._log('browser_pdf_network.jsonl', {'url': url, 'status': resp.status,
                                                           'mime_type': mime, 'method': resp.request.method,
                                                           'source_page': page.url})
                    if allowed_host(url, self.hosts) and resp.status == 200:
                        self.queue(url, page.url, '', '', 'browser_network')
                        # When the public download endpoint requires POST, its
                        # *existing* response is captured; never replay POST.
                        if resp.request.method != 'GET' or 'application/pdf' in mime:
                            self.browser_pdf_responses.append((resp, page.url))
                elif 'application/json' in mime and resp.status == 200 and allowed_host(url, self.hosts):
                    # Read only small public API responses. No login or API replay.
                    self.last_network_activity.append({'url': url, 'mime_type': mime[:100]})
                    if len(self.last_network_activity) > 150:
                        self.last_network_activity.pop(0)
                    size = int(hdrs.get('content-length') or 0)
                    if size < 2_000_000:
                        try:
                            body = resp.body()
                            if len(body) >= 2_000_000:
                                return
                            for found in extract_json_links(json.loads(body), url):
                                self.queue(found, page.url, '', '', 'public_json_response')
                        except Exception as exc:
                            LOG.debug('Could not decode public JSON response %s: %s', url, exc)
            except Exception as exc:
                LOG.debug('Response inspection failed: %s', exc)

        def on_download(download):
            self.browser_downloads.append((download, page.url))

        page.on('response', on_response)
        page.on('download', on_download)

    def _drain_browser_downloads(self):
        while self.browser_pdf_responses and len(self.entries) < self.args.max_files:
            resp, source = self.browser_pdf_responses.pop(0)
            try:
                if not allowed_host(resp.url, self.hosts) or not self.allowed_by_robots(resp.url):
                    continue
                size = int(resp.headers.get('content-length') or 0)
                if size > self.args.max_mb * 1024**2:
                    raise ValueError('Browser PDF response exceeds configured max size')
                raw = resp.body()
                if len(raw) > self.args.max_mb * 1024**2:
                    raise ValueError('Browser PDF body exceeds configured max size')
                item = Candidate(resp.url, source, '', '', 'browser_pdf_response')
                self._store(raw, item, resp.headers.get('content-type', 'application/pdf'))
            except Exception as exc:
                self.download_failures += 1
                self._log('errors.jsonl', {'stage': 'browser_pdf_response', 'url': resp.url, 'error': str(exc)})
        while self.browser_downloads and len(self.entries) < self.args.max_files:
            download, source = self.browser_downloads.pop(0)
            path = self.debug_dir / ('browser_' + hashlib.sha256((download.url + source).encode()).hexdigest()[:15] + '.part')
            try:
                if not allowed_host(download.url, self.hosts):
                    continue
                if not self.allowed_by_robots(download.url):
                    continue
                download.save_as(str(path))
                if path.stat().st_size > self.args.max_mb * 1024**2:
                    raise ValueError('Browser download exceeds configured max size')
                item = Candidate(download.url, source, '', download.suggested_filename, 'browser_download')
                self._store(path.read_bytes(), item, 'application/pdf', download.suggested_filename)
            except Exception as exc:
                self.download_failures += 1
                self._log('errors.jsonl', {'stage': 'browser_download', 'url': download.url, 'error': str(exc)})
            finally:
                path.unlink(missing_ok=True)

    def _try_download_buttons(self, page):
        """Click only explicit public PDF/download controls, never forms or login."""
        try:
            count = min(15, page.locator('button, a[onclick], [role="button"]').count())
            # Iterate over all candidates up to a bounded number, selecting by visible label.
            buttons = page.locator('button, a[onclick], [role="button"]')
            total = min(buttons.count(), 100)
            clicked = 0
            for i in range(total):
                if clicked >= count:
                    break
                el = buttons.nth(i)
                try:
                    label = (el.inner_text(timeout=1500) or '').lower().strip()
                    if not label or not any(k in label for k in DOWNLOAD_BUTTON_WORDS):
                        continue
                    if any(k in label for k in DANGEROUS_WORDS):
                        continue
                    href = el.get_attribute('href')
                    if href and not href.startswith(('#', 'javascript:')):
                        # These are handled as ordinary links.
                        continue
                    if not el.is_visible():
                        continue
                    LOG.info('Clicking public download control: %r', label[:80])
                    clicked += 1
                    el.click(timeout=3500)
                    page.wait_for_timeout(700)
                    self._drain_browser_downloads()
                except Exception as exc:
                    LOG.debug('Download button click issue: %s', exc)
        except Exception:
            pass

    def _browser_scan(self, page, label: str):
        self.ui_pages_scanned += 1
        url = page.url
        html = page.content()
        self.feed_html(html, url, label)
        if self.args.save_html:
            dest = self.debug_dir / ('page_' + hashlib.sha256(label.encode()).hexdigest()[:14] + '.html')
            dest.write_text(html, encoding='utf-8')
        try:
            # Bounded infinite-scroll discovery: stop when page height stabilizes.
            previous_height = 0
            for scroll_index in range(4):
                height = page.evaluate('document.documentElement.scrollHeight')
                if height <= previous_height:
                    break
                previous_height = height
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(max(700, self.args.browser_wait_ms))
                changed = page.content()
                if changed != html:
                    self.feed_html(changed, page.url, label + ':scroll-' + str(scroll_index+1))
                    html = changed
        except Exception:
            pass

    def _click_next(self, page) -> bool:
        # Supports pages that keep the same URL while replacing rows with JS.
        script = '''() => {
           let els = [...document.querySelectorAll('a,button,[role="button"]')];
           for(let e of els){
             const t = (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().toLowerCase();
             const cls = ((e.getAttribute('class')||'')+' '+(e.parentElement?.getAttribute('class')||'')).toLowerCase();
             if(e.disabled || e.getAttribute('aria-disabled')==='true' || cls.includes('disabled')) continue;
             if(e.getAttribute('rel')==='next' || cls.includes('pagination-next') || cls.includes('next-page') ||
                (['بعدی','بعد','صفحه بعد','صفحه بعدی','next','›','»','→'].includes(t) &&
                 (cls.includes('page') || cls.includes('pagination') || cls.includes('next') || e.tagName==='BUTTON')) ||
                ((t.includes('next page') || t.includes('صفحه بعد')) &&
                 (cls.includes('page') || cls.includes('pagination') || e.tagName==='BUTTON'))){
               e.setAttribute('data-wiki-next','true'); return true;
             }
           }
           // Some JS paginators offer only numbered buttons (no Next control).
           const nav = [...document.querySelectorAll('.pagination,[class*="pagination"],[aria-label*="Pagination"]')];
           for (let el of nav) {
             const active = el.querySelector('.active,[aria-current="page"]');
             if (!active) continue;
             const current = parseInt((active.innerText || '').trim(), 10);
             if (!Number.isFinite(current)) continue;
             const candidates = [...el.querySelectorAll('a,button,[role="button"]')];
             const next = candidates.find(x => parseInt((x.innerText || '').trim(), 10) === current+1 &&
                 !x.disabled && x.getAttribute('aria-disabled')!=='true');
             if (next) {next.setAttribute('data-wiki-next','true'); return true;}
           }
           return false;
         }'''
        try:
            if not page.evaluate(script):
                return False
            page.locator('[data-wiki-next="true"]').first.click(timeout=6000)
            page.wait_for_load_state('domcontentloaded', timeout=8000)
            page.wait_for_timeout(self.args.browser_wait_ms)
            return True
        except Exception as exc:
            LOG.debug('JS next click failed: %s', exc)
            return False

    def run_browser(self):
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as p:
            browser_path = (self.args.browser_executable or
                            os.environ.get('WIKI_CRAWLER_BROWSER_PATH') or None)
            if browser_path:
                browser_path = os.path.abspath(os.path.expanduser(browser_path))
                if not os.path.isfile(browser_path) or not os.access(browser_path, os.X_OK):
                    raise RuntimeError(f'Browser executable not found/executable: {browser_path}')
                LOG.info('Using local Chromium-compatible browser: %s', browser_path)
            try:
                browser = p.chromium.launch(headless=not self.args.headful,
                                            executable_path=browser_path)
            except Exception as exc:
                raise RuntimeError(
                    'Could not launch Chromium. Install a local Chrome/Chromium '
                    'and pass --browser-executable /path/to/browser; '
                    f'underlying Playwright error: {exc}'
                ) from exc
            context = browser.new_context(user_agent=UA, accept_downloads=True,
                                           locale='fa-IR', viewport={'width': 1440, 'height': 900})
            context.on('page', self._capture_browser)
            page = context.new_page()
            self._capture_browser(page)
            try:
                while self.pending_pages and self.ui_pages_scanned < self.args.max_pages and len(self.entries) < self.args.max_files:
                    url = self.pending_pages.popleft()
                    if url in self.visited:
                        continue
                    if not self.allowed_by_robots(url):
                        LOG.warning('Robots disallows HTML page %s', url)
                        continue
                    try:
                        self._throttle()
                        response = page.goto(url, wait_until='domcontentloaded', timeout=self.args.timeout * 1000)
                        page.wait_for_timeout(self.args.browser_wait_ms)
                        if not allowed_host(page.url, self.hosts):
                            LOG.error('Browser redirected outside allowed site to %s; skipping', page.url)
                            self.visited.add(url)
                            continue
                        if response and response.status in (401,403,429):
                            LOG.error('Access restriction HTTP %s, stopping', response.status)
                            return
                        if response and ('application/pdf' in response.headers.get('content-type','') or PDF_RE.search(page.url)):
                            self.queue(page.url, url, '', '', 'browser_navigation')
                            self.visited.add(url)
                            self.ui_pages_scanned += 1
                            continue
                        # No CAPTCHA/login bypass; if the site requires authentication, log evidence.
                        if re.search(r'(?i)(captcha required|access denied|verify you are human)', page.title()):
                            LOG.error('Access-control page detected, stopping')
                            return
                        self.visited.add(url)
                        # Carry over anonymous public browsing-session cookies
                        # so file GETs use the same session as the browser.
                        try:
                            for cookie in context.cookies():
                                domain = cookie.get('domain','').lstrip('.')
                                if domain in self.hosts:
                                    self.session.cookies.set(cookie['name'], cookie['value'],
                                                             domain=domain, path=cookie.get('path','/'))
                        except Exception:
                            pass
                        fingerprints: set[str] = set()
                        for n in range(self.args.max_ui_pages):
                            # Count rendered JS pagination states as pages, not just URL changes.
                            if self.ui_pages_scanned >= self.args.max_pages:
                                break
                            fingerprint = hashlib.sha256(page.content().encode()).hexdigest()
                            if fingerprint in fingerprints:
                                break
                            fingerprints.add(fingerprint)
                            self._browser_scan(page, url + '#ui-' + str(n+1))
                            # Process downloads before advancing pagination.
                            self._drain_browser_downloads()
                            self.drain_files()
                            if len(self.entries) >= self.args.max_files:
                                break
                            before_click_url = page.url
                            self._try_download_buttons(page)
                            self._drain_browser_downloads()
                            self.drain_files()
                            if len(self.entries) >= self.args.max_files:
                                break
                            if page.url != before_click_url:
                                self.queue(page.url, before_click_url, '', '', 'button_navigation')
                                break
                            self._throttle()
                            if not self._click_next(page):
                                break
                        self._drain_browser_downloads()
                        self.drain_files()
                    except Exception as exc:
                        LOG.warning('Browser page error for %s: %s', url, exc)
                        self._log('errors.jsonl', {'stage': 'browser_page', 'url': url, 'error': str(exc)})
                self._drain_browser_downloads()
                self.drain_files()
            finally:
                (self.debug_dir / 'network_json_urls.json').write_text(json.dumps(self.last_network_activity, ensure_ascii=False, indent=2), encoding='utf-8')
                browser.close()

    def run_static(self):
        while self.pending_pages and len(self.visited) < self.args.max_pages and len(self.entries) < self.args.max_files:
            url = self.pending_pages.popleft()
            if url in self.visited:
                continue
            self.visited.add(url)
            try:
                resp = self._direct_get(url)
                mime = resp.headers.get('Content-Type', '')
                if 'pdf' in mime.lower() or PDF_RE.search(resp.url):
                    self.queue(url, url, '', '', 'static_navigation')
                else:
                    resp.encoding = resp.apparent_encoding or 'utf-8'
                    self.feed_html(resp.text, resp.url)
                    if self.args.save_html:
                        (self.debug_dir / ('page_' + hashlib.sha256(url.encode()).hexdigest()[:14] + '.html')).write_text(resp.text, encoding='utf-8')
                resp.close()
                self.drain_files()
            except Exception as exc:
                LOG.warning('Static page error %s: %s', url, exc)
                self._log('errors.jsonl', {'stage': 'static_page', 'url': url, 'error': str(exc)})
        self.drain_files()

    def drain_files(self):
        while self.pending_files and len(self.entries) < self.args.max_files:
            item = self.pending_files.popleft()
            try:
                self._save_download(item)
            except Exception as exc:
                self.download_failures += 1
                LOG.warning('Download error %s: %s', item.url, exc)
                self._log('errors.jsonl', {'stage': 'download', 'url': item.url,
                                           'source_page': item.source_page, 'error': str(exc)})

    def run(self):
        try:
            if not self.allowed_by_robots(self.base):
                raise RuntimeError('robots.txt inaccessible/disallows start URL; crawler stopped')
            if self.args.mode == 'browser':
                self.run_browser()
            else:
                self.run_static()
        finally:
            self._save_manifest()
            report = {'start': self.base, 'mode': self.args.mode,
                      'pages_visited': len(self.visited),
                      'rendered_pages_scanned': self.ui_pages_scanned,
                      'new_pdfs_in_run': len(self.entries) - self.initial_file_count,
                      'pdf_candidates_seen': self.pdf_candidates_seen,
                      'pdfs_downloaded': len(self.entries),
                      'duplicates_in_run': self.duplicate_count,
                      'download_errors_in_run': self.download_failures,
                      'target': self.args.max_files, 'finished_utc': now()}
            (self.out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            LOG.info('SUMMARY: %s', report)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='Crawl public organizational PDFs from IranFOIA')
    ap.add_argument('--start', default=DEFAULT_START)
    ap.add_argument('--out', default='data/iranfoia')
    ap.add_argument('--mode', choices=('browser', 'static'), default='browser')
    ap.add_argument('--max-files', type=int, default=150)
    ap.add_argument('--max-pages', type=int, default=350, help='total rendered pages in browser mode (URL and JS pagination states)')
    ap.add_argument('--max-ui-pages', type=int, default=100, help='pagination clicks on a single JS listing')
    ap.add_argument('--scope', choices=('documents', 'same-host'), default='documents')
    ap.add_argument('--allow-host', action='append', default=[], help='explicitly allow an extra host for linked PDFs')
    ap.add_argument('--delay', type=float, default=1.5)
    ap.add_argument('--timeout', type=int, default=25)
    ap.add_argument('--browser-wait-ms', type=int, default=1100)
    ap.add_argument('--max-mb', type=int, default=35)
    ap.add_argument('--headful', action='store_true')
    ap.add_argument('--browser-executable', default='', help='Use a local Chrome/Chromium binary instead of downloading Playwright Chromium')
    ap.add_argument('--save-html', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.max_files < 1 or args.max_pages < 1 or args.max_ui_pages < 1 or args.max_mb < 1 or args.delay < 0.5:
        sys.exit('max-files/pages must be >=1, delay must be >=0.5 seconds')
    try:
        Crawler(args).run()
    except (RuntimeError, requests.RequestException) as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
