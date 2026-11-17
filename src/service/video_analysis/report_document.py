"""Dependency-free incremental PDF evidence writer for the global model."""
from __future__ import annotations

import json
from pathlib import Path


def _pdf_escape(value: str) -> str:
    value = value.encode("ascii", "backslashreplace").decode("ascii")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_pdf(path: Path, lines: list[str]) -> None:
    page_size = 48
    pages = [lines[i:i + page_size] for i in range(0, max(1, len(lines)), page_size)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
    ]
    page_numbers = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    for index, page_lines in enumerate(pages):
        page_number = 4 + index * 2
        content_number = page_number + 1
        stream_lines = ["BT", "/F1 7 Tf", "50 790 Td", "9 TL"]
        for line in page_lines:
            stream_lines.append(f"({_pdf_escape(line[:180])}) Tj")
            stream_lines.append("0 -1 TD")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("ascii")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(output)


class IncrementalEvidenceDocument:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lines = [
            "CapCut Mate video scene evidence",
            "Incrementally generated evidence for global model summarization.",
        ]
        self.flush()

    def append_scene(self, scene: dict) -> None:
        self.lines.extend([
            "",
            f"Scene {scene.get('scene_index', '?')}",
            json.dumps(scene, ensure_ascii=True, indent=2),
        ])
        self.flush()

    def append_context(self, transcript: str, transitions: list[dict]) -> None:
        self.lines.extend([
            "",
            "Transcript",
            transcript or "unavailable",
            "",
            "Transitions",
            json.dumps(transitions, ensure_ascii=True, indent=2),
        ])
        self.flush()

    def flush(self) -> None:
        _write_pdf(self.path, self.lines)
