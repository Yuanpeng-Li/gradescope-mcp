import copy
import json
from types import SimpleNamespace
import pytest
from gradescope_mcp.tools import outline


@pytest.fixture
def setup(monkeypatch):
    tree = [{"id": 1, "title": "Q1", "weight": 5, "crop_rect_list": []}]
    calls = []

    def patch(url, **kwargs):
        calls.append((url, kwargs))
        tree[:] = copy.deepcopy(kwargs["json"]["question_data"])
        return SimpleNamespace(status_code=200)

    conn = SimpleNamespace(session=SimpleNamespace(patch=patch))
    monkeypatch.setattr(outline, "get_connection", lambda: conn)
    monkeypatch.setattr(
        outline,
        "_read",
        lambda *a: (
            {"assignment": {"id_regions": {"name": None, "sid": None}}},
            False,
            copy.deepcopy(tree),
            "rev",
            "token",
            "https://example.com/courses/1/assignments/2/outline/edit",
        ),
    )
    return tree, calls


def test_preview_and_write(setup):
    tree, calls = setup
    proposed = copy.deepcopy(tree)
    proposed[0]["title"] = "Updated"
    proposed.append({"title": "New", "weight": 2, "crop_rect_list": []})
    args = ("1", "2", json.dumps(proposed))
    assert "No changes" in outline.update_assignment_outline(*args)
    assert not calls
    assert "expected_revision" in outline.update_assignment_outline(
        *args, confirm_write=True
    )
    assert not calls
    assert "verified" in outline.update_assignment_outline(
        *args, expected_revision="rev", confirm_write=True
    )
    assert calls[0][1]["json"]["assignment"]["identification_regions"] == {
        "name": None,
        "sid": None,
    }


def test_stale_and_deletion(setup):
    tree, calls = setup
    assert "changed" in outline.update_assignment_outline(
        "1", "2", json.dumps(tree), "stale", True
    )
    assert "deletion" in outline.update_assignment_outline("1", "2", "[]", "rev", True)
    assert not calls


@pytest.mark.parametrize("weight", [True, -1, float("nan"), float("inf")])
def test_invalid_weight(weight):
    with pytest.raises(ValueError):
        outline._validate([{"title": "bad", "weight": weight}], False)


def test_online_payload(monkeypatch):
    current = [
        {
            "id": 1,
            "title": "One",
            "weight": 3,
            "type": "OnlineQuestion",
            "parent_id": None,
            "index": 1,
            "content": [],
        }
    ]
    proposed = copy.deepcopy(current)
    proposed[0]["content"] = [
        {"type": "text", "value": "Prompt"},
        {"type": "explanation", "value": "Reference"},
    ]
    reads = iter([current, proposed])
    props = {"saveUrl": "/courses/1/assignments/2/outline", "baseHash": "hash"}
    monkeypatch.setattr(
        outline,
        "_read",
        lambda *a: (
            props,
            True,
            next(reads),
            "rev",
            "csrf",
            "https://example.com/courses/1/assignments/2/outline/edit",
        ),
    )
    calls = []

    def patch(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        outline,
        "get_connection",
        lambda: SimpleNamespace(session=SimpleNamespace(patch=patch)),
    )
    assert "verified" in outline.update_assignment_outline(
        "1", "2", json.dumps(proposed), "rev", True
    )
    assert json.loads(calls[0]["data"]["outline"]) == proposed
    assert calls[0]["data"]["base_hash"] == "hash"
    assert calls[0]["data"]["ignore_warnings"] is False


def test_read_online_editor_and_revision(monkeypatch):
    import html

    props = {
        "questions": {
            "1": {
                "id": 1,
                "title": "Group",
                "type": "QuestionGroup",
                "weight": 2,
                "index": 1,
                "parent_id": None,
                "content": [],
            },
            "2": {
                "id": 2,
                "title": "Part",
                "type": "OnlineQuestion",
                "weight": 2,
                "index": 1,
                "parent_id": 1,
                "content": [],
            },
        },
        "baseHash": "one",
        "assignment": {},
    }

    def get(url):
        page = (
            '<meta name="csrf-token" content="csrf"><div data-react-class="AssignmentEditor" data-react-props="'
            + html.escape(json.dumps(props), quote=True)
            + '"></div>'
        )
        return SimpleNamespace(status_code=200, text=page)

    conn = SimpleNamespace(
        gradescope_base_url="https://example.com", session=SimpleNamespace(get=get)
    )
    first = outline._read(conn, "1", "2")
    assert first[2][0]["children"][0]["id"] == 2
    props["baseHash"] = "two"
    assert outline._read(conn, "1", "2")[3] != first[3]
    props["questions"]["2"]["parent_id"] = 99
    with pytest.raises(ValueError, match="orphaned"):
        outline._read(conn, "1", "2")


def test_omitting_existing_crops_is_refused(setup):
    tree, calls = setup
    q = dict(tree[0])
    del q["crop_rect_list"]
    assert "Preserve crop_rect_list" in outline.update_assignment_outline(
        "1", "2", json.dumps([q]), "rev", True
    )
    assert not calls


def test_readback_failure_reports_accepted_write(setup, monkeypatch):
    tree, calls = setup
    read = outline._read

    def later(*args):
        if calls:
            raise ValueError("unavailable")
        return read(*args)

    monkeypatch.setattr(outline, "_read", later)
    result = outline.update_assignment_outline("1", "2", json.dumps(tree), "rev", True)
    assert result.startswith("⚠️") and "accepted" in result


@pytest.mark.parametrize(
    "rect",
    [
        None,
        {"x1": 0, "x2": 101, "y1": 0, "y2": 2, "page_number": 1},
        {"x1": 0, "x2": 1, "y1": 0, "y2": 2, "page_number": 0},
    ],
)
def test_bad_regions(rect):
    with pytest.raises(ValueError):
        outline._validate(
            [{"title": "Q", "weight": 1, "crop_rect_list": [rect]}], False
        )


@pytest.mark.parametrize(
    "save_url",
    [
        "http://example.com/courses/1/assignments/2/outline",
        "https://evil.example/courses/1/assignments/2/outline",
        "/courses/1/assignments/99/outline",
        "/courses/1/assignments/2/outline?target=99",
    ],
)
def test_save_url_is_bound_to_assignment(monkeypatch, save_url):
    tree = [
        {
            "id": 1,
            "title": "Q",
            "weight": 1,
            "type": "OnlineQuestion",
            "index": 1,
            "parent_id": None,
            "content": [],
        }
    ]
    monkeypatch.setattr(outline, "get_connection", lambda: object())
    monkeypatch.setattr(
        outline,
        "_read",
        lambda *a: (
            {"saveUrl": save_url, "baseHash": "b"},
            True,
            tree,
            "rev",
            "csrf",
            "https://example.com/courses/1/assignments/2/outline/edit",
        ),
    )
    assert "Unexpected" in outline.update_assignment_outline(
        "1", "2", json.dumps(tree), "rev", True
    )


@pytest.mark.parametrize("online", [False, True])
def test_null_question_id_is_rejected(online):
    node = {
        "id": None,
        "title": "Q",
        "weight": 1,
        "type": "OnlineQuestion",
        "index": 1,
        "parent_id": None,
        "content": [],
    }
    with pytest.raises(ValueError, match="IDs"):
        outline._validate([node], online)


def test_duplicate_online_indices_are_rejected():
    node = {
        "id": 1,
        "title": "Q",
        "weight": 1,
        "type": "OnlineQuestion",
        "index": 1,
        "parent_id": None,
        "content": [],
    }
    with pytest.raises(ValueError, match="indices"):
        outline._validate([node, dict(node, id=-1)], True)
