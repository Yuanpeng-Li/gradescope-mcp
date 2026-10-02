"""The crop-page selection rule shared by the grading context and the workflow tools."""

from __future__ import annotations

import pytest

from gradescope_mcp.tools import grading_ops
from gradescope_mcp.tools import grading_workflow as gw
from gradescope_mcp.tools.common import page_number, select_crop_pages


def _pages(*numbers):
    return [{"number": n, "url": f"https://s3.example/p{n}.jpg"} for n in numbers]


@pytest.mark.parametrize("crop, expected", [
    ([], [1, 2, 3, 4, 5, 6]),            # no crop info: every page
    ([3], [2, 3, 4]),                    # crop page and its neighbours
    ([1, 6], [1, 2, 5, 6]),
    (["4"], [3, 4, 5]),                  # numeric strings count
    ([9], [1, 2, 3, 4, 5, 6]),           # crop page not in the submission: every page
    ([7], [1, 2, 3, 4, 5, 6]),           # ... even when a neighbour (6) exists
    ([2, 9], [1, 2, 3]),                 # partial match keeps the matching neighbourhood
    ([None, "x"], [1, 2, 3, 4, 5, 6]),   # unusable crop info is ignored
])
def test_select_crop_pages(crop, expected) -> None:
    selected = select_crop_pages(_pages(1, 2, 3, 4, 5, 6), crop)
    assert [p["number"] for p in selected] == expected


def test_select_crop_pages_without_pages() -> None:
    assert select_crop_pages([], [1]) == []


def test_page_number() -> None:
    assert [page_number(v) for v in (3, "3", " 4 ", None, "x", 2.0)] == [3, 3, 4, None, None, 2]


@pytest.mark.parametrize("crop", [[], [3], [1, 6], [9], [7], [2, 9]])
def test_grading_context_and_workflow_select_the_same_pages(crop) -> None:
    pages = _pages(1, 2, 3, 4, 5, 6)
    rects = [{"page_number": n} for n in crop]

    context_pages, crop_numbers, real_count = grading_ops._select_context_pages(pages, rects)
    workflow_pages = gw._select_relevant_pages(pages, rects)

    assert context_pages == workflow_pages
    assert crop_numbers == sorted(crop) and real_count == 6
