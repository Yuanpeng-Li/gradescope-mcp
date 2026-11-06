"""Local PDF previews and page-completeness checks; no Gradescope mutation."""

import io
import json
import hashlib

from pypdf import PdfReader, PdfWriter

from gradescope_mcp.cache import get_artifact_path, write_artifact
from gradescope_mcp.tools.outline import _read, _validate
from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.submissions import _validate_upload_path, _upload_roots


def preview_outline_regions(
    course_id: str, assignment_id: str, template_path: str, outline_json: str = ""
) -> str:
    try:
        from reportlab.pdfgen.canvas import Canvas

        source = _validate_upload_path(template_path, _upload_roots()).read_bytes()
        if not source.startswith(b"%PDF-"):
            raise ValueError("Expected a PDF template")
        if outline_json:
            tree = json.loads(outline_json)
        else:
            _, online, tree, _, _, _ = _read(get_connection(), course_id, assignment_id)
            if online:
                raise ValueError("Online assignments do not have PDF crop regions")
        _validate(tree, False)
        reader = PdfReader(io.BytesIO(source))
        writer = PdfWriter(clone_from=reader)
        regions = {}
        warnings = []

        def visit(nodes):
            for index, q in enumerate(nodes, 1):
                rects = q.get("crop_rect_list", [])
                if not rects and not q.get("children"):
                    warnings.append(f"Question {q.get('id', index)} has no regions")
                for rect in rects:
                    page = int(rect["page_number"])
                    if page > len(reader.pages):
                        raise ValueError(f"Region page {page} exceeds PDF page count")
                    regions.setdefault(page, []).append((str(q.get("id", index)), rect))
                visit(q.get("children", []))

        visit(tree)
        for number, page in enumerate(writer.pages, 1):
            page.transfer_rotation_to_content()
            box = page.cropbox
            left, bottom = float(box.left), float(box.bottom)
            width, height = float(box.width), float(box.height)
            overlay = io.BytesIO()
            canvas = Canvas(
                overlay, pagesize=(float(page.mediabox.right), float(page.mediabox.top))
            )
            for qid, r in regions.get(number, []):
                x = left + width * r["x1"] / 100
                y = bottom + height * (1 - r["y2"] / 100)
                canvas.setStrokeColorRGB(0.85, 0.1, 0.1)
                canvas.setFillColorRGB(0.85, 0.1, 0.1)
                canvas.setLineWidth(1.2)
                canvas.rect(
                    x,
                    y,
                    width * (r["x2"] - r["x1"]) / 100,
                    height * (r["y2"] - r["y1"]) / 100,
                    fill=0,
                )
                canvas.setFont("Helvetica", 8)
                canvas.drawString(
                    x + 2,
                    min(
                        bottom + height - 9, y + height * (r["y2"] - r["y1"]) / 100 - 9
                    ),
                    "Q " + qid,
                )
            canvas.save()
            page.merge_page(PdfReader(io.BytesIO(overlay.getvalue())).pages[0])
        output = io.BytesIO()
        writer.write(output)
        digest = hashlib.sha256(
            source + json.dumps(tree, sort_keys=True).encode()
        ).hexdigest()[:16]
        path = write_artifact(
            get_artifact_path(f"outline-preview-{assignment_id}-{digest}.pdf"),
            output.getvalue(),
        )
        return json.dumps(
            {
                "path": str(path),
                "pages": len(reader.pages),
                "region_count": sum(map(len, regions.values())),
                "warnings": warnings,
                "notice": "Local visual preview only. Inspect every page before approving coordinate writes.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def inspect_submission_pdf(
    file_path: str, required_pages: list[int] | None = None
) -> str:
    try:
        path = _validate_upload_path(file_path, _upload_roots())
        data = path.read_bytes()
        reader = PdfReader(io.BytesIO(data))
        required = required_pages or []
        if any(type(n) is not int or n < 1 for n in required):
            raise ValueError("Pages must be positive integers")
        return json.dumps(
            {
                "pages": len(reader.pages),
                "sha256": hashlib.sha256(data).hexdigest(),
                "missing_required_pages": sorted(
                    set(n for n in required if n > len(reader.pages))
                ),
                "dimensions": [
                    {
                        "page": i + 1,
                        "width": float(p.cropbox.width),
                        "height": float(p.cropbox.height),
                        "rotation": p.rotation,
                    }
                    for i, p in enumerate(reader.pages)
                ],
                "notice": "Page-count check only; blank, illegible or semantically missing answers require visual review.",
            }
        )
    except Exception as exc:
        return f"Error: {exc}"
