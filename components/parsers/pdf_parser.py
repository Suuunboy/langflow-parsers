"""Langflow component that extracts text and images from a PDF file.

The text keeps ``<imageN>`` placeholders where images appear on the page, and
the images are returned separately as ``{"imageN": "<base64 PNG>"}``.
A downstream vision model can describe each image and splice the description
back into the text before chunking and indexing.

The file is intentionally self-contained (everything lives inside the component
class): Langflow loads custom components one file at a time, and the code can
also be pasted as-is into the Custom Component editor.
"""

import base64
import hashlib
import os
import re
from io import BytesIO

import pymupdf
from langflow.custom import Component
from langflow.io import DataInput, DropdownInput, IntInput, Output
from langflow.schema import Data
from PIL import Image


class PdfParser(Component):
    display_name = 'PDF Parser'
    description = 'Extract text with <imageN> placeholders and PNG images from a PDF file.'
    icon = 'file-type'
    name = 'PdfParser'

    READING_ORDER_ROWS = 'Top to bottom'
    READING_ORDER_COLUMNS = 'Two columns'

    inputs = [
        DataInput(
            name='pdf_data',
            display_name='PDF Data',
            info="Data with the file path in 'value' (list of paths) or 'file_path'.",
            tool_mode=True,
        ),
        IntInput(
            name='min_image_size',
            display_name='Min Image Size (px)',
            info='Skip images whose width or height is below this value '
            '(icons, bullets, separators). Set to 0 to keep every image.',
            value=50,
            advanced=True,
        ),
        DropdownInput(
            name='reading_order',
            display_name='Reading Order',
            info="'Top to bottom' suits single-column documents, forms and borderless "
            "tables. 'Two columns' reads the left column before the right one.",
            options=[READING_ORDER_ROWS, READING_ORDER_COLUMNS],
            value=READING_ORDER_ROWS,
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            name='text_output',
            display_name='Text with image tags',
            method='build_text',
        ),
        Output(
            name='images_output',
            display_name='Images dict',
            method='build_images',
        ),
        Output(
            name='collection_name_output',
            display_name='Collection name',
            method='build_collection_name',
        ),
    ]

    # A block crossing the page centre by less than this fraction of the page
    # width still counts as belonging to one column.
    _COLUMN_TOLERANCE = 0.02

    # ---------- outputs ----------

    def build_text(self) -> Data:
        text, _ = self._parse_pdf()
        path = self._source_path()
        filename = self._display_filename(path) if path else ''
        return Data(data={'filename': filename, 'text': text})

    def build_images(self) -> Data:
        _, images = self._parse_pdf()
        return Data(value=images)

    def build_collection_name(self) -> str:
        return str(getattr(self.pdf_data, 'collection_name', None) or '')

    # ---------- input handling ----------

    def _source_paths(self) -> list[str]:
        """Input file paths: 'value' (custom loaders) or 'file_path' (File component)."""
        data = self.pdf_data
        raw = getattr(data, 'value', None) or getattr(data, 'file_path', None)
        if not raw:
            return []
        if isinstance(raw, (str, os.PathLike)):
            raw = [raw]
        return [os.fspath(p) for p in raw]

    def _source_path(self) -> str | None:
        paths = self._source_paths()
        return paths[0] if paths else None

    @staticmethod
    def _display_filename(path: str) -> str:
        """Last two path components: Langflow stores uploads as '<flow_id>/<file>'."""
        parts = [p for p in re.split(r'[\\/]+', path) if p]
        return '/'.join(parts[-2:])

    def _warn(self, msg: str) -> None:
        try:
            self.log(msg)
        except Exception:
            print(msg)

    # ---------- parsing ----------

    def _parse_pdf(self) -> tuple[str, dict[str, str]]:
        paths = self._source_paths()
        if not paths:
            return '', {}

        min_size = max(int(self.min_image_size or 0), 0)
        two_columns = self.reading_order == self.READING_ORDER_COLUMNS

        # Every output method calls this on the same instance: parse once.
        key = (paths[0], min_size, two_columns)
        cache = getattr(self, '_parsed_cache', None)
        if cache is not None and cache[0] == key:
            return cache[1], cache[2]

        if len(paths) > 1:
            self._warn(
                f'[PdfParser] {len(paths)} files received, only the first one is parsed: {paths[0]}'
            )

        images: dict[str, str] = {}
        tags: dict[str, str | None] = {}  # sha1 of raw image bytes -> tag (None if skipped)
        pages: list[str] = []

        with pymupdf.open(paths[0]) as doc:
            for page in doc:
                blocks = page.get_text('dict')['blocks']
                if two_columns:
                    blocks = self._two_column_order(blocks, page.rect.width)
                else:
                    blocks = sorted(blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))

                lines: list[str] = []
                for block in blocks:
                    if block['type'] == 0:  # text
                        for line in block.get('lines', []):
                            lines.append(''.join(span['text'] for span in line['spans']))
                    elif block['type'] == 1:  # image
                        tag = self._tag_for_image(block['image'], images, tags, min_size)
                        if tag is not None:
                            lines.append(f'<{tag}>')
                pages.append('\n'.join(lines))

        text = '\n\n'.join(pages).strip()
        self._parsed_cache = (key, text, images)
        return text, images

    @classmethod
    def _two_column_order(cls, blocks: list[dict], page_width: float) -> list[dict]:
        """Order blocks so that a two-column layout is read column by column.

        Text blocks are split into single lines first: MuPDF may merge lines
        of both columns that share a baseline into one block. Items are then
        walked top to bottom. An item crossing the vertical centre of the page
        (a title, a full-width paragraph or figure) closes the current band;
        inside a band the left column goes before the right one.
        """
        items: list[dict] = []
        for block in blocks:
            if block['type'] == 0:
                items += [
                    {'type': 0, 'bbox': line['bbox'], 'lines': [line]}
                    for line in block.get('lines', [])
                ]
            else:
                items.append(block)

        mid = page_width / 2
        tolerance = page_width * cls._COLUMN_TOLERANCE
        ordered: list[dict] = []
        left: list[dict] = []
        right: list[dict] = []
        for block in sorted(items, key=lambda b: (b['bbox'][1], b['bbox'][0])):
            x0, _, x1, _ = block['bbox']
            if x1 <= mid + tolerance:
                left.append(block)
            elif x0 >= mid - tolerance:
                right.append(block)
            else:
                ordered += left + right + [block]
                left, right = [], []
        return ordered + left + right

    # ---------- images ----------

    def _tag_for_image(self, blob: bytes, images: dict, tags: dict, min_size: int) -> str | None:
        """Tag of an image; a repeated image (e.g. a logo on every page) shares one tag."""
        digest = hashlib.sha1(blob).hexdigest()
        if digest not in tags:
            png = self._png_via_pillow(blob, min_size)
            if png is None:
                png = self._png_via_mupdf(blob, min_size)
            tag = None
            if png:
                tag = f'image{len(images) + 1}'
                images[tag] = png
            tags[digest] = tag
        return tags[digest]

    @staticmethod
    def _pil_to_png_b64(im: Image.Image, min_size: int = 0) -> str:
        """PIL image -> base64 PNG in RGB, transparency flattened onto white.

        Returns '' (not None) for images below min_size, so that the caller
        can tell "too small" from "cannot decode".
        """
        if im.width < min_size or im.height < min_size:
            return ''
        has_alpha = im.mode in ('RGBA', 'LA', 'PA') or (
            im.mode == 'P' and 'transparency' in im.info
        )
        if has_alpha:
            im = im.convert('RGBA')
            bg = Image.new('RGB', im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != 'RGB':
            # CMYK JPEGs and grayscale scans end up here.
            im = im.convert('RGB')
        buf = BytesIO()
        im.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('ascii')

    def _png_via_pillow(self, blob: bytes, min_size: int = 0) -> str | None:
        try:
            with Image.open(BytesIO(blob)) as im:
                im.load()
                return self._pil_to_png_b64(im, min_size)
        except Exception:
            return None

    def _png_via_mupdf(self, blob: bytes, min_size: int = 0) -> str | None:
        """Fallback for formats Pillow cannot decode (e.g. JBIG2): let MuPDF rasterize them."""
        try:
            pix = pymupdf.Pixmap(blob)
            if pix.width < min_size or pix.height < min_size:
                return ''
            if pix.colorspace is not None and pix.colorspace.n != 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            return base64.b64encode(pix.tobytes('png')).decode('ascii')
        except Exception as e:
            self._warn(f'[PdfParser] image skipped, cannot decode: {type(e).__name__}: {e}')
            return None
