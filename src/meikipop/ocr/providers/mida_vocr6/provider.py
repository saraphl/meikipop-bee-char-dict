# meikipop/ocr/providers/mida_vocr6/provider.py
"""mida v2 line detection + VOCR6 recognition, fully local on the CPU. Horizontal lines are read by
VOCR6.2-L; vertical columns are rotated anticlockwise and read by VOCR6-V.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import numpy as np
import onnxruntime as ort
from PIL import Image

from meikipop.ocr.interface import BoundingBox, OcrProvider, Paragraph, Word
from meikipop.ocr.providers.postprocessing import group_lines_into_paragraphs
from . import models
from . import vocr_onnx as V
from .line_crops import DEFAULT_H, DEFAULT_V, LineCrop, cell_boxes, crop_image, make_crops
from .mida_detector import MidaDetector

logger = logging.getLogger(__name__)

JAPANESE_REGEX = re.compile(r'[぀-ゟ゠-ヿ一-龯]')

THREADS = min(4, os.cpu_count() or 1)


class MidaVocr6Provider(OcrProvider):
    NAME = "mida + VOCR6 (local)"

    def __init__(self):
        logger.info(f"initializing {self.NAME} provider...")
        self.detector = MidaDetector(models.ensure_mida(), threads=THREADS)
        self.recognizer = V.Recognizer(models.ensure_vocr(), self._rec_options(), cal=V.CHARBOX_CAL)
        self.v_recognizer = V.Recognizer(models.ensure_vocr_v(), self._rec_options())
        self._pool = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="vocr-line")
        logger.info(f"{self.NAME} initialized with {THREADS} threads")

    @staticmethod
    def _rec_options():
        so = ort.SessionOptions()
        so.intra_op_num_threads = so.inter_op_num_threads = 1
        so.log_severity_level = 3
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return so

    def scan(self, image: Image.Image) -> Optional[List[Paragraph]]:
        try:
            rgb = np.asarray(image.convert("RGB"))
            H, W = rgb.shape[:2]
            if W == 0 or H == 0:
                logger.error("invalid image dimensions received.")
                return None

            lines = self.detector.detect(rgb)
            crops = make_crops(lines, DEFAULT_H, DEFAULT_V, W, H)
            return group_lines_into_paragraphs([p for p in self._read_lines(rgb, crops) if p is not None])

        except Exception as e:
            logger.error(f"an error occurred in {self.NAME}: {e}", exc_info=True)
            return None

    def _read_lines(self, rgb: np.ndarray, crops: List[LineCrop]) -> List[Optional[Paragraph]]:
        H, W = rgb.shape[:2]
        reads = self._pool.map(lambda crop: self._recognize(rgb, crop), crops)
        lines = [(crop, chars) for crop, chars in zip(crops, reads) if chars]
        self._refine(rgb, lines)
        return [_paragraph(crop, chars, W, H) for crop, chars in lines]

    def _refine(self, rgb: np.ndarray, lines: List[Tuple[LineCrop, List[V.Char]]]):
        """Hook for subclasses: correct the recognized characters in place before paragraphs are built."""

    def _recognize(self, rgb: np.ndarray, crop: LineCrop) -> Optional[List[V.Char]]:
        try:
            grey = V.grey_of(crop_image(rgb, crop))
            rec = self.v_recognizer if crop.vertical else self.recognizer
            return rec.read(grey, invert=V.line_dark(grey))
        except Exception as e:
            logger.warning(f"could not read the line at {crop.box}: {e}")
            return None


def _paragraph(crop: LineCrop, chars: List[V.Char], W: int, H: int) -> Optional[Paragraph]:
    """One recognized line as a Paragraph of one Word per character, or None without kana or kanji."""
    full_text = "".join(c.text for c in chars)
    if not JAPANESE_REGEX.search(full_text):
        return None
    words = [Word(text=c.text, separator="", box=_norm(*box, W=W, H=H))
             for c, box in zip(chars, cell_boxes(crop, [(c.x0, c.x1) for c in chars]))]
    x0, y0, x1, y1 = crop.box
    return Paragraph(full_text=full_text, words=words, box=_norm(x0, y0, x1, y1, W, H), is_vertical=crop.vertical)


def _norm(x0: float, y0: float, x1: float, y1: float, W: int, H: int) -> BoundingBox:
    """A pixel box as a normalized meikipop BoundingBox (center and size, 0.0 to 1.0)."""
    return BoundingBox(center_x=(x0 + x1) / 2 / W, center_y=(y0 + y1) / 2 / H, width=(x1 - x0) / W,
                       height=(y1 - y0) / H)
