import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pypdf import PdfWriter
from crawl import Crawler, parse_args


def make_pdf():
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(buf)
    return buf.getvalue()


PDF_A = make_pdf()


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlsplit(self.path)
        if p.path == '/robots.txt':
            mime, data = 'text/plain', b'User-agent: *\nAllow: /\n'
        elif p.path == '/document/orgs/' and p.query == '':
            mime = 'text/html; charset=utf-8'
            data = '<h1>فهرست اسناد</h1><a href="?page=2">بعدی</a><a href="/files/a.pdf">PDF</a>'.encode()
        elif p.path == '/document/orgs/' and p.query == 'page=2':
            mime, data = 'text/html; charset=utf-8', b'<a href="/document/123">View document</a>'
        elif p.path == '/document/123':
            mime, data = 'text/html; charset=utf-8', b'<a href="/file/download/42">Download PDF</a>'
        elif p.path in ('/files/a.pdf', '/file/download/42'):
            mime, data = 'application/pdf', PDF_A
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_real_multi_page_crawl_dedup(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        start = f'http://127.0.0.1:{server.server_port}/document/orgs/'
        args = parse_args(['--start', start, '--mode', 'static', '--out', str(tmp_path),
                           '--max-pages', '8', '--max-files', '5', '--delay', '0.5'])
        crawler = Crawler(args)
        crawler.run()
        stats = json.loads((tmp_path / 'summary.json').read_text())
        assert stats['pages_visited'] == 3
        assert stats['pdfs_downloaded'] == 1, 'identical PDF bodies should be deduplicated by SHA256'
        assert len(list((tmp_path / 'pdfs').glob('*.pdf'))) == 1
        assert (tmp_path / 'manifest.csv').exists()
        assert (tmp_path / 'duplicates.jsonl').exists()
    finally:
        server.shutdown()
        server.server_close()
