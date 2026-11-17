"""PaddleOCR PP-OCRv5 adapter and temporal text aggregation."""
from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any


class PaddleOCRAdapter:
    def __init__(self, min_confidence: float = 0.45) -> None:
        from paddleocr import PaddleOCR

        self.min_confidence = min_confidence
        self.engine = PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    @staticmethod
    def _as_dict(result: Any) -> dict:
        if isinstance(result, dict):
            return result.get("res", result)
        data = getattr(result, "json", None)
        if callable(data):
            data = data()
        if isinstance(data, str):
            import json
            data = json.loads(data)
        return (data or {}).get("res", data or {})

    def recognize(self, frame) -> list[dict]:
        results = self.engine.predict(frame)
        if not results:
            return []
        data = self._as_dict(results[0])
        texts = data.get("rec_texts", []) or []
        scores = data.get("rec_scores", []) or []
        polygons = data.get("dt_polys", []) or []
        items = []
        for index, text in enumerate(texts):
            value = re.sub(r"\s+", " ", str(text)).strip()
            score = float(scores[index]) if index < len(scores) else 0.0
            if not value:
                continue
            if score < self.min_confidence:
                continue
            if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", value):
                continue
            items.append({
                "text": value,
                "confidence": round(score, 4),
                "polygon": polygons[index].tolist() if index < len(polygons) and hasattr(polygons[index], "tolist") else [],
            })
        return items


def aggregate_text(frame_results: list[list[dict]], timestamp_us: int) -> list[dict]:
    grouped: list[dict] = []
    for items in frame_results:
        for item in items:
            text = item["text"]
            match = next(
                (
                    current for current in grouped
                    if SequenceMatcher(None, current["text"], text).ratio() >= 0.88
                ),
                None,
            )
            if match:
                match["scores"].append(item.get("confidence", 0.0))
                match["observations"] += 1
                if item.get("confidence", 0.0) > match["best_confidence"]:
                    match["text"] = text
                    match["best_confidence"] = item.get("confidence", 0.0)
            else:
                grouped.append({
                    "text": text,
                    "scores": [item.get("confidence", 0.0)],
                    "best_confidence": item.get("confidence", 0.0),
                    "observations": 1,
                })
    aggregated = [
        {
            "text": item["text"],
            "confidence": round(sum(item["scores"]) / len(item["scores"]), 4),
            "observations": item["observations"],
            "timestamp_us": timestamp_us,
            "source": "paddleocr",
        }
        for item in grouped
    ]
    # Public OCR is a readable summary, not a dump of every PaddleOCR box.
    # Keep stable detections, or very high-confidence one-frame text, and
    # discard tiny latin fragments that are usually packaging noise.
    filtered = []
    for item in aggregated:
        text = item["text"]
        if len(text.strip()) <= 1 and not re.search(r"[\u4e00-\u9fff0-9]", text):
            continue
        if item["observations"] < 2 and item["confidence"] < 0.82:
            continue
        filtered.append(item)
    filtered.sort(
        key=lambda item: (item["observations"], item["confidence"], len(item["text"])),
        reverse=True,
    )
    return filtered[:20]
