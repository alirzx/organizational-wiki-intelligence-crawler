import pytest

from crawl import Candidate, Crawler, parse_args
from test_integration import PDF_A


def test_default_output():
    args = parse_args([])
    assert args.out == 'data/iranfoia'
    assert args.mode == 'browser'
    assert args.max_files == 150


def test_invalid_pdf_header_is_not_enough(tmp_path):
    args = parse_args(['--start', 'https://iranfoia.ir/document/list/', '--out', str(tmp_path)])
    crawler = Crawler(args)
    with pytest.raises(ValueError, match='Unreadable/invalid PDF'):
        crawler._store(b'%PDF-1.4\n' + b'not a complete PDF' * 7,
                       Candidate(args.start + 'broken.pdf', args.start), 'application/pdf')
    assert not list((tmp_path / 'pdfs').glob('*.pdf'))


def test_resume_rejects_changed_pdf(tmp_path):
    args = parse_args(['--start', 'https://iranfoia.ir/document/list/', '--out', str(tmp_path)])
    first = Crawler(args)
    assert first._store(PDF_A, Candidate(args.start + 'good.pdf', args.start), 'application/pdf')
    assert Crawler(args).initial_file_count == 1
    file_path = next((tmp_path / 'pdfs').glob('*.pdf'))
    file_path.write_bytes(b'X' * len(PDF_A))
    resumed = Crawler(args)
    assert resumed.initial_file_count == 0
    assert not resumed.entries
