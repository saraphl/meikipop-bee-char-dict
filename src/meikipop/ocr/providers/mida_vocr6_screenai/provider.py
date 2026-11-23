# meikipop/ocr/providers/mida_vocr6_screenai/provider.py
"""mida + VOCR6, with Chrome Screen AI as a second opinion: when VOCR6 is unsure of some kana or kanji, Screen AI reads
the area around those lines once and replaces each such character where it reads a different one there more confidently.
"""
import logging
import re
from typing import List, NamedTuple, Set, Tuple

import numpy as np
from PIL import Image

from meikipop.ocr.providers.mida_vocr6 import vocr_onnx as V
from meikipop.ocr.providers.mida_vocr6.line_crops import LineCrop, cell_boxes
from meikipop.ocr.providers.mida_vocr6.provider import MidaVocr6Provider
from meikipop.ocr.providers.screenai.provider import ScreenAiOcr

logger = logging.getLogger(__name__)

THRESHOLD = 0.85  # VOCR6 kana and kanji below this confidence get a second opinion
MARGIN = 0.0  # Screen AI must beat VOCR6's confidence by more than this to replace a character
PAD = 0.5  # context around each unsure line for Screen AI, in line thicknesses

KANA_KANJI = re.compile(r'[぀-ゟァ-ヺー-ヿ㐀-䶿一-鿿豈-﫿々〆〇]')  # without ゠ and ・


class Symbol(NamedTuple):
    """A Screen AI character: its center and the center of its line, in screenshot pixels."""
    text: str
    conf: float
    x: float
    y: float
    line_x: float
    line_y: float


class MidaVocr6ScreenAiProvider(MidaVocr6Provider):
    NAME = "mida + VOCR6 + Screen AI (local)"

    def __init__(self):
        super().__init__()
        self.screen_ai = ScreenAiOcr()
        self._logged: Set[str] = set()  # the last scan's replacements, so auto scan doesn't repeat them

    def _refine(self, rgb: np.ndarray, lines: List[Tuple[LineCrop, List[V.Char]]]):
        unsure = [(crop, chars) for crop, chars in lines if any(_unsure(c) for c in chars)]
        replaced = []
        if unsure:
            try:
                symbols = self._read_around(rgb, [crop for crop, _ in unsure])
                replaced = [r for crop, chars in unsure for r in _merge(crop, chars, symbols)]
            except Exception as e:
                logger.warning(f"Screen AI second opinion failed: {e}")
        for r in replaced:
            if r not in self._logged:
                logger.info(r)
        self._logged = set(replaced)

    def _read_around(self, rgb: np.ndarray, crops: List[LineCrop]) -> List[Symbol]:
        # one Screen AI call costs ~100 ms whatever the size, so read the box around all unsure lines at once
        H, W = rgb.shape[:2]
        boxes = []
        for crop in crops:
            x0, y0, x1, y1 = crop.box
            pad = int(PAD * (x1 - x0 if crop.vertical else y1 - y0))
            boxes.append((x0 - pad, y0 - pad, x1 + pad, y1 + pad))
        ox, oy = max(0, min(b[0] for b in boxes)), max(0, min(b[1] for b in boxes))
        ex, ey = min(W, max(b[2] for b in boxes)), min(H, max(b[3] for b in boxes))
        result = self.screen_ai.annotate(Image.fromarray(rgb[oy:ey, ox:ex]))
        return [Symbol(s.utf8_string, s.confidence, ox + s.bounding_box.x + s.bounding_box.width / 2,
                       oy + s.bounding_box.y + s.bounding_box.height / 2,
                       ox + line.bounding_box.x + line.bounding_box.width / 2,
                       oy + line.bounding_box.y + line.bounding_box.height / 2)
                for line in result.lines for word in line.words for s in word.symbols]


def _unsure(c: V.Char) -> bool:
    return c.conf < THRESHOLD and bool(KANA_KANJI.fullmatch(c.text))


def _merge(crop: LineCrop, chars: List[V.Char], symbols: List[Symbol]) -> List[str]:
    """Replace each unsure character by the Screen AI character of the same line nearest its cell's center, if more
    confident; returns the replacements as log lines."""
    x0, y0, x1, y1 = crop.box
    ours = [s for s in symbols if (x0 <= s.line_x < x1 if crop.vertical else y0 <= s.line_y < y1)]
    before = "".join(c.text for c in chars)
    replaced = []
    for c, (cx0, cy0, cx1, cy1) in zip(chars, cell_boxes(crop, [(c.x0, c.x1) for c in chars])):
        if not _unsure(c):
            continue
        inside = [s for s in ours if cx0 <= s.x < cx1 and cy0 <= s.y < cy1]
        if not inside:
            logger.debug(f"Screen AI: nothing at {c.text} ({c.conf:.2f}) in {before}")
            continue
        mx, my = (cx0 + cx1) / 2, (cy0 + cy1) / 2
        s = min(inside, key=lambda s: (s.x - mx) ** 2 + (s.y - my) ** 2)
        if s.text != c.text and s.conf > c.conf + MARGIN and KANA_KANJI.fullmatch(s.text):
            replaced.append(f"Screen AI: {c.text} ({c.conf:.2f}) -> {s.text} ({s.conf:.2f}) in {before}")
            c.text, c.conf = s.text, s.conf
        else:
            logger.debug(f"Screen AI: kept {c.text} ({c.conf:.2f}) over {s.text} ({s.conf:.2f}) in {before}")
    return replaced
