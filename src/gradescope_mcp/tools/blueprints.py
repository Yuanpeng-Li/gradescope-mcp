"""Compose a reviewed assignment copy from supported creation/outline/rubric primitives."""

import copy
import json
from urllib.parse import urlsplit

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.cache import get_artifact_path, write_artifact
from gradescope_mcp.tools import assignment_admin as admin, outline
from gradescope_mcp.tools.rubric_plans import (
    _rubric,
    prepare_rubric_plan,
    execute_rubric_plan,
)
from gradescope_mcp.tools.question_settings import _scoring, set_question_scoring
from gradescope_mcp.tools.submission_files import _download
from gradescope_mcp.tools.safety import question_write_lock, write_confirmation_required


def _source(c, a):
    conn = get_connection()
    soup, fields, _ = admin._settings(conn, c, a)
    props, online, tree, revision, _, _ = outline._read(conn, c, a)
    leaves = []

    def visit(nodes):
        for q in nodes:
            if q.get("children"):
                visit(q["children"])
            else:
                leaves.append(str(q["id"]))

    visit(tree)
    rubrics = {qid: _rubric(c, qid) for qid in leaves}
    scoring = {qid: _scoring(c, qid)[1] for qid in leaves}
    return (
        soup,
        fields,
        tree,
        online,
        {
            "revision": revision,
            "rubrics": rubrics,
            "scoring": scoring,
            "identification_regions": None
            if online
            else (props.get("assignment") or {}).get("id_regions"),
        },
    )


def prepare_assignment_clone(
    course_id: str, assignment_id: str, new_title: str, settings: dict | None = None
) -> str:
    try:
        if not new_title.strip():
            raise ValueError("New title required")
        soup, fields, tree, online, basis = _source(course_id, assignment_id)
        typ = fields.get("assignment[type]")
        kind = (
            "online"
            if online
            else "programming"
            if typ == "ProgrammingAssignment"
            else "bubble_sheet"
            if fields.get("assignment[bubble_sheet]") == "true"
            else "homework"
            if fields.get("assignment[submission_type]") == "image"
            else "exam"
        )
        options = {
            k[len("assignment[") : -1]: v
            for k, v in fields.items()
            if k.startswith("assignment[")
            and k[len("assignment[") : -1] in admin._SETTING_KEYS
        }
        options.update(settings or {})
        options.update(kind=kind, title=new_title)
        template = None
        if kind in ("exam", "homework"):
            links = [
                a["href"]
                for a in soup.select("a[href]")
                if urlsplit(a["href"]).path.lower().endswith(".pdf")
            ]
            if not links:
                raise ValueError("Source template download unavailable")
            data = _download(get_connection(), links[0])
            if not data.startswith(b"%PDF-"):
                raise ValueError("Source template is not PDF")
            template = str(
                write_artifact(
                    get_artifact_path(
                        "clone-template-"
                        + ops.digest([course_id, assignment_id, basis])[:20]
                        + ".pdf"
                    ),
                    data,
                )
            )
        request, token = admin._prepare(
            get_connection(), course_id, None, "create", options, template, True
        )
        new_tree = copy.deepcopy(tree)
        next_id = -1

        def remap(nodes, parent=None):
            nonlocal next_id
            for q in nodes:
                if online:
                    q["id"] = next_id
                    next_id -= 1
                    q["parent_id"] = parent
                else:
                    q.pop("id", None)
                    q.pop("parent_id", None)
                remap(q.get("children", []), q.get("id"))

        remap(new_tree)
        req = {
            "course_id": course_id,
            "source_assignment_id": assignment_id,
            "create": request,
            "options": options,
            "template_path": template,
            "outline": new_tree,
            "source_outline": tree,
            "rubrics": basis["rubrics"],
            "scoring": basis["scoring"],
            "identification_regions": basis.get("identification_regions"),
        }
        plan = ops.create("assignment_clone", req, basis, [])
        return json.dumps(
            {
                "operation_token": plan["operation_token"],
                "request_digest": plan["request_digest"],
                "preview": req,
                "notice": "Creates a new assignment; copies settings/dates, template, outline and flat rubric entries. Does not copy student submissions, grades, LMS links, autograder images or group constraints. Review copied dates before approving. Partial failures retain the new assignment for inspection.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def execute_assignment_clone(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    try:
        with question_write_lock("operation", operation_token):
            op = ops.load(operation_token)
            r = op["request"]
            if (
                op["kind"] != "assignment_clone"
                or request_digest != op["request_digest"]
            ):
                raise ValueError("Plan kind/digest mismatch")
            if not confirm_write:
                return write_confirmation_required(
                    "execute_assignment_clone",
                    [
                        json.dumps(r),
                        f"operation_token={operation_token}",
                        f"request_digest={request_digest}",
                    ],
                )
            if op["status"] != "prepared":
                raise ValueError(
                    "Clone already attempted; inspect operation rather than duplicating again"
                )
            if ops.cancelled(operation_token):
                op["status"] = "cancelled"
                ops.save(op)
                return json.dumps(op)
            _, _, _, _, basis = _source(r["course_id"], r["source_assignment_id"])
            if basis != op["snapshots"]:
                raise ValueError("Source assignment changed")
            fresh, _ = admin._prepare(
                get_connection(),
                r["course_id"],
                None,
                "create",
                r["options"],
                r["template_path"],
                True,
            )
            if fresh != r["create"]:
                raise ValueError("Creation capabilities or source PDF changed")
            create_req = {
                **fresh,
                "course_id": r["course_id"],
                "assignment_id": None,
                "action": "create",
                "options": r["options"],
                "template_path": r["template_path"],
                "trusted_template": True,
            }
            child = ops.create("assignment", create_req, fresh["baseline"], [])
            op["status"] = "running"
            op["creation_operation"] = child["operation_token"]
            ops.save(op)
            result = admin.execute_assignment_plan(
                child["operation_token"], child["request_digest"], True
            )
            created = ops.load(child["operation_token"])
            aid = created.get("created_assignment_id")
            if created["status"] != "verified" or not aid:
                raise ValueError(
                    "Assignment creation not verified; inspect child operation"
                )
            op["created_assignment_id"] = aid
            ops.save(op)
            if ops.cancelled(operation_token):
                op["status"] = "cancelled"
                ops.save(op)
                return json.dumps(op)
            _, _, _, revision, _, _ = outline._read(
                get_connection(), r["course_id"], aid
            )
            result = outline.update_assignment_outline(
                r["course_id"],
                aid,
                json.dumps(r["outline"]),
                revision,
                True,
                r.get("identification_regions"),
            )
            op["outline_result"] = result
            ops.save(op)
            if result != "Assignment outline saved and verified.":
                raise ValueError("Copied outline was not verified")
            _, _, saved, _, _, _ = outline._read(get_connection(), r["course_id"], aid)

            def leaves(nodes):
                return [
                    q
                    for node in nodes
                    for q in (
                        leaves(node["children"]) if node.get("children") else [node]
                    )
                ]

            old, new = leaves(r["source_outline"]), leaves(saved)
            if len(old) != len(new):
                raise ValueError("Copied question count differs")
            op["rubric_results"] = []
            for source, target in zip(old, new):
                if ops.cancelled(operation_token):
                    op["status"] = "cancelled"
                    ops.save(op)
                    return json.dumps(op)
                qid = str(target["id"])
                original = r["scoring"][str(source["id"])]
                _, before, _ = _scoring(r["course_id"], qid)
                result = set_question_scoring(
                    r["course_id"],
                    qid,
                    original["scoring_type"],
                    original["ceiling"],
                    original["floor"],
                    ops.digest(before),
                    True,
                )
                op["rubric_results"].append(
                    {"question_id": qid, "scoring_result": result}
                )
                ops.save(op)
                if not result.startswith(
                    ("Question scoring saved and verified", "Question already has")
                ):
                    raise ValueError("Copied scoring options not verified")
                desired = [
                    {k: item[k] for k in ("description", "weight")}
                    for item in r["rubrics"].get(str(source["id"]), [])
                ]
                prepared = prepare_rubric_plan(r["course_id"], qid, desired, True)
                if prepared.startswith(("Error:", "Authentication error:")):
                    raise ValueError(prepared)
                plan = json.loads(prepared)
                op["rubric_results"][-1]["operation_token"] = plan["operation_token"]
                ops.save(op)
                if ops.cancelled(operation_token):
                    ops.cancel_operation(plan["operation_token"])
                    op["status"] = "cancelled"
                    ops.save(op)
                    return json.dumps(op)
                result = execute_rubric_plan(
                    plan["operation_token"], plan["request_digest"], True
                )
                if result.startswith(("Error:", "Authentication error:")):
                    raise ValueError(result)
                if json.loads(result)["status"] != "completed":
                    raise ValueError(
                        "Copied rubric not fully verified; inspect child operation"
                    )
            op["status"] = "verified"
            op["notice"] = (
                "Creation, outline, per-question scoring and flat rubric entries verified. Rubric group constraints, LMS links, autograder configuration and student data are not copied."
            )
            ops.save(op)
            return json.dumps(op)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        if "op" in locals() and op.get("status") == "running":
            op["status"] = "needs_review"
            op["error"] = str(exc)
            ops.save(op)
        return f"Error: {exc}. Query the clone operation; partial new assignment may exist."
