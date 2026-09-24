import base64
import re
from io import BytesIO

import pymupdf
import pytest
from langflow.schema import Data
from PIL import Image

from parsers import pdf_parser
from parsers.pdf_parser import PdfParser

from conftest import decode_b64, make_png

# ---------- document builders ----------


def new_pdf(pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()  # A4-ish: 595 x 842 pt
    return doc


def save(doc, tmp_path, name='doc.pdf') -> str:
    path = tmp_path / 'flow-id' / name
    path.parent.mkdir(exist_ok=True)
    doc.save(path)
    doc.close()
    return str(path)


def parser_for(path, **kwargs) -> PdfParser:
    data = Data(data={'value': [path], 'collection_name': 'my_collection'})
    return PdfParser(pdf_data=data, **kwargs)


def tags_in(text: str) -> list[str]:
    return re.findall(r'<(image\d+)>', text)


def make_cmyk_jpeg(width=120, height=80) -> bytes:
    buf = BytesIO()
    Image.new('CMYK', (width, height), (0, 255, 255, 0)).save(buf, format='JPEG')
    return buf.getvalue()


# ---------- text and placeholders ----------


def test_text_and_images_in_reading_order(tmp_path):
    doc = new_pdf()
    page = doc[0]
    page.insert_text((72, 72), 'Title')
    page.insert_image(pymupdf.Rect(72, 100, 272, 233), stream=make_png(color=(255, 0, 0)))
    page.insert_text((72, 300), 'Caption below the image')
    parser = parser_for(save(doc, tmp_path))

    text = parser.build_text().data['text']
    images = parser.build_images().data['value']

    assert text == 'Title\n<image1>\nCaption below the image'
    assert decode_b64(images['image1']).getpixel((0, 0)) == (255, 0, 0)


def test_pages_are_separated_by_blank_line(tmp_path):
    doc = new_pdf(pages=2)
    doc[0].insert_text((72, 72), 'Page one')
    doc[1].insert_text((72, 72), 'Page two')
    parser = parser_for(save(doc, tmp_path))

    assert parser.build_text().data['text'] == 'Page one\n\nPage two'


TITLE = 'Full width title that crosses the centre of the page and spans both columns'
FOOTER = 'Full width footer that crosses the centre of the page and spans both columns'


def two_column_pdf(tmp_path) -> str:
    doc = new_pdf()
    page = doc[0]
    page.insert_text((50, 60), TITLE)
    page.insert_text((50, 120), 'Left one')
    page.insert_text((320, 120), 'Right one')
    page.insert_text((50, 300), 'Left two')
    page.insert_text((320, 300), 'Right two')
    page.insert_text((50, 500), FOOTER)
    return save(doc, tmp_path)


def test_default_order_is_top_to_bottom(tmp_path):
    parser = parser_for(two_column_pdf(tmp_path))

    lines = parser.build_text().data['text'].splitlines()

    assert lines[1:5] == ['Left one', 'Right one', 'Left two', 'Right two']


def test_two_column_order(tmp_path):
    parser = parser_for(two_column_pdf(tmp_path), reading_order=PdfParser.READING_ORDER_COLUMNS)

    lines = parser.build_text().data['text'].splitlines()

    assert lines[0].startswith('Full width title')
    assert lines[1:5] == ['Left one', 'Left two', 'Right one', 'Right two']
    assert lines[5].startswith('Full width footer')


# ---------- image handling ----------


def test_repeated_image_shares_one_tag(tmp_path):
    doc = new_pdf(pages=2)
    logo = make_png(color=(0, 0, 255))
    for page in doc:
        page.insert_image(pymupdf.Rect(72, 72, 172, 139), stream=logo)
    parser = parser_for(save(doc, tmp_path))

    assert tags_in(parser.build_text().data['text']) == ['image1', 'image1']
    assert list(parser.build_images().data['value']) == ['image1']


def test_small_images_are_skipped(tmp_path):
    doc = new_pdf()
    doc[0].insert_image(pymupdf.Rect(72, 72, 82, 82), stream=make_png(10, 10))
    doc[0].insert_image(pymupdf.Rect(72, 100, 272, 200), stream=make_png(200, 100))
    path = save(doc, tmp_path)

    default = parser_for(path)
    keep_all = parser_for(path, min_image_size=0)

    assert tags_in(default.build_text().data['text']) == ['image1']
    assert decode_b64(default.build_images().data['value']['image1']).size == (200, 100)
    assert tags_in(keep_all.build_text().data['text']) == ['image1', 'image2']


def test_cmyk_jpeg_is_normalized_to_rgb_png(tmp_path):
    doc = new_pdf()
    doc[0].insert_image(pymupdf.Rect(72, 72, 192, 152), stream=make_cmyk_jpeg())
    parser = parser_for(save(doc, tmp_path))

    b64 = parser.build_images().data['value']['image1']
    image = decode_b64(b64)

    assert base64.b64decode(b64).startswith(b'\x89PNG')
    assert image.mode == 'RGB'


def test_mupdf_fallback_produces_rgb_png():
    png = PdfParser()._png_via_mupdf(make_cmyk_jpeg(120, 80))

    image = decode_b64(png)
    assert image.format == 'PNG'
    assert image.mode == 'RGB'
    assert image.size == (120, 80)


def test_undecodable_image_is_skipped_with_warning(warnings_of):
    parser = PdfParser()
    messages = warnings_of(parser)

    tag = parser._tag_for_image(b'not an image', {}, {}, 0)

    assert tag is None
    assert 'cannot decode' in messages[0]


# ---------- input and outputs ----------


@pytest.mark.parametrize('data', [None, Data(data={'value': []}), Data(data={})])
def test_empty_input(data):
    parser = PdfParser(pdf_data=data)

    assert parser.build_text().data == {'filename': '', 'text': ''}
    assert parser.build_images().data['value'] == {}
    assert parser.build_collection_name() == ''


def test_filename_and_collection_name(tmp_path):
    parser = parser_for(save(new_pdf(), tmp_path, 'report.pdf'))

    assert parser.build_text().data['filename'] == 'flow-id/report.pdf'
    assert parser.build_collection_name() == 'my_collection'


def test_accepts_file_path_from_file_component(tmp_path):
    doc = new_pdf()
    doc[0].insert_text((72, 72), 'Hello')
    parser = PdfParser(pdf_data=Data(data={'file_path': save(doc, tmp_path)}))

    assert parser.build_text().data['text'] == 'Hello'


def test_only_first_file_is_parsed(tmp_path, warnings_of):
    first, second = new_pdf(), new_pdf()
    first[0].insert_text((72, 72), 'first')
    second[0].insert_text((72, 72), 'second')
    paths = [save(first, tmp_path, 'a.pdf'), save(second, tmp_path, 'b.pdf')]
    parser = PdfParser(pdf_data=Data(data={'value': paths}))
    messages = warnings_of(parser)

    assert parser.build_text().data['text'] == 'first'
    assert len(messages) == 1


def test_document_is_parsed_once_for_all_outputs(tmp_path, monkeypatch):
    doc = new_pdf()
    doc[0].insert_text((72, 72), 'Hello')
    parser = parser_for(save(doc, tmp_path))
    calls = []
    real_open = pdf_parser.pymupdf.open
    monkeypatch.setattr(pdf_parser.pymupdf, 'open', lambda p: calls.append(p) or real_open(p))

    parser.build_text()
    parser.build_images()

    assert len(calls) == 1
