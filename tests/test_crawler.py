from crawl import extract_links, normalize, kind_for, pdf_ok, safe_name


def test_url_normalization():
    assert normalize('../download?id=1', 'https://iranfoia.ir/document/orgs/7') == 'https://iranfoia.ir/document/download?id=1'
    assert normalize('javascript:void(0)', 'https://iranfoia.ir') is None
    assert normalize('#next', 'https://iranfoia.ir') is None


def test_links_and_pagination():
    html = '''<html><head><title>اسناد سازمان</title></head><body>
    <a href="/document/orgs/?page=2">بعدی</a>
    <a href="/document/123">متن بخشنامه</a>
    <a href="/media/archive/paper.pdf">PDF</a>
    <button onclick="window.open('/document/download/9')">دانلود فایل</button>
    <iframe src="/public/file.pdf"></iframe></body></html>'''
    links = extract_links(html, 'https://iranfoia.ir/document/orgs/')
    urls = {x['url'] for x in links}
    assert 'https://iranfoia.ir/document/orgs/?page=2' in urls
    assert 'https://iranfoia.ir/media/archive/paper.pdf' in urls
    assert 'https://iranfoia.ir/document/download/9' in urls
    assert kind_for('https://iranfoia.ir/document/download/9') == 'file'
    assert kind_for('https://iranfoia.ir/document/123') == 'page'


def test_pdf_validation():
    assert pdf_ok(b'%PDF-1.4\n' + b'a'*32)
    assert not pdf_ok(b'<html>sign in</html>')
    assert safe_name('../my form') == 'my_form.pdf'


def test_json_discovery():
    from crawl import extract_json_links
    data = {'records': [{'fileUrl': '/file/download/41', 'title': 'سند'}, {'url': '/x.pdf'}], 'other': 'ignored'}
    links = extract_json_links(data, 'https://iranfoia.ir/document/orgs/')
    assert links == ['https://iranfoia.ir/file/download/41', 'https://iranfoia.ir/x.pdf']
