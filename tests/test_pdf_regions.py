import json
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from pypdf import PdfReader
from gradescope_mcp.tools.pdf_regions import (
    preview_outline_regions,
    inspect_submission_pdf,
)


def test_preview_overlay_and_page_validation(tmp_path):
    source = tmp_path / "exam.pdf"
    c = Canvas(str(source), pagesize=(600, 800))
    c.drawString(60, 720, "Synthetic exam: answer below")
    c.save()
    before = source.read_bytes()
    tree = [
        {
            "id": 1,
            "title": "Q",
            "weight": "4.0",
            "crop_rect_list": [
                {"page_number": 1, "x1": 10, "y1": 15, "x2": 90, "y2": 40}
            ],
        }
    ]
    result = json.loads(
        preview_outline_regions("1", "2", str(source), json.dumps(tree))
    )
    assert result["pages"] == 1 and result["region_count"] == 1
    assert "Q 1" in PdfReader(result["path"]).pages[0].extract_text()
    assert source.read_bytes() == before
    tree[0]["crop_rect_list"][0]["page_number"] = 2
    assert "exceeds" in preview_outline_regions("1", "2", str(source), json.dumps(tree))
    assert json.loads(inspect_submission_pdf(str(source), [1, 2]))[
        "missing_required_pages"
    ] == [2]
