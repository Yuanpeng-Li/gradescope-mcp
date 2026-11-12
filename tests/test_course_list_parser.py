"""list_courses parses the account page itself (offline HTML fixtures).

Regression for a staff account whose list_courses said "No courses found"
although it could read a known course's assignments: gradescopeapi's parser
only follows ``<a>`` siblings after a term heading and decides the role by
the "Create Course" button alone.
"""

from types import SimpleNamespace

import pytest

from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import courses

BASE = "https://gs.test"


def box(cid, short, name="Full Name", count="3 assignments", cls="courseBox"):
    return (
        f'<a class="{cls}" href="/courses/{cid}">'
        f'<h3 class="courseBox--shortname">{short}</h3>'
        f'<div class="courseBox--name">{name}</div>'
        f'<div class="courseBox--assignments">{count}</div></a>'
    )


def page(body, create=False):
    button = '<button class="js-createNewCourse">Create</button>' if create else ""
    return f"<html><body>{button}<div id='account-show'>{body}</div></body></html>"


# Classic layout: course boxes are direct siblings after the term heading.
CLASSIC = page(
    '<h2 class="pageHeading">Instructor Courses</h2>'
    '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
    + box("111", "CS 1", "Intro")
    + box("112", "CS 2", "Data")
    + '</div><h2 class="pageHeading">Student Courses</h2>'
    '<div class="courseList"><div class="courseList--term">Spring 2025</div>'
    + box("221", "MATH 9", "Calc", "1 assignment")
    + "</div>",
    create=True,
)

# Wrapped layout: each term's boxes sit in nested containers.
WRAPPED = page(
    '<div class="pageHeader"><h2 class="pageHeading">Instructor Courses</h2></div>'
    '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
    '<div class="courseList--coursesForTerm"><div class="row"><div class="col">'
    + box("111", "CS 1")
    + '</div><div class="col">'
    + box("112", "CS 2")
    + '</div></div></div><div class="courseList--term">Winter 2026</div>'
    '<div class="courseList--coursesForTerm"><div>'
    + box("113", "CS 3")
    + "</div></div></div>",
    create=True,
)

# A TA: staff section, no Create Course button, boxes without the courseBox
# class but with the short-name heading.
STAFF_NO_BUTTON = page(
    '<h2 class="pageHeading">Course Staff</h2>'
    '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
    '<div class="courseList--coursesForTerm">'
    + box("555", "STAT 7", "Stats", cls="")
    + "</div></div>"
)

STUDENT_ONLY = page(
    '<h2 class="pageHeading">Student Courses</h2>'
    '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
    + box("777", "BIO 1", "Biology")
    + "</div>"
)

EMPTY = page("")

# Course links exist but in no recognisable course box.
UNPARSEABLE = page(
    '<div class="newCourseGrid">'
    '<a class="tile" href="/courses/1"><span>CS 1</span></a>'
    '<a class="tile" href="/courses/2"><span>CS 2</span></a></div>'
)

# Course boxes whose name element changed.
UNREADABLE_BOXES = page(
    '<h2 class="pageHeading">Instructor Courses</h2>'
    '<div class="courseList"><a class="courseBox" href="/courses/9">'
    '<div class="courseBox--title">CS 9</div></a></div>'
)


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        status, text = self.pages.get(url[len(BASE):], (404, "missing"))
        return SimpleNamespace(status_code=status, text=text)


def run(monkeypatch, pages):
    session = FakeSession(pages)
    conn = SimpleNamespace(gradescope_base_url=BASE, session=session)
    monkeypatch.setattr(courses, "get_connection", lambda: conn)
    return courses.list_courses(), session


def test_classic_layout_groups_instructor_and_student(monkeypatch):
    text, session = run(monkeypatch, {"/account": (200, CLASSIC)})
    assert session.urls == [BASE + "/account"]
    instructor, student = text.split("## Student Courses")
    assert instructor.startswith("## Instructor Courses\n")
    assert "- **CS 1** (Intro)\n  - ID: `111`\n  - Semester: Fall 2026\n  - Assignments: 3 assignments" in instructor
    assert "`112`" in instructor
    assert "- **MATH 9** (Calc)\n  - ID: `221`\n  - Semester: Spring 2025\n  - Assignments: 1 assignment" in student


def test_wrapped_layout_finds_every_box_and_its_term(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, WRAPPED)})
    assert text.count("- **CS ") == 3
    assert "`111`\n  - Semester: Fall 2026" in text
    assert "`112`\n  - Semester: Fall 2026" in text
    assert "`113`\n  - Semester: Winter 2026" in text
    assert "Student Courses" not in text


def test_staff_without_create_button_is_listed_as_instructor_staff(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, STAFF_NO_BUTTON)})
    assert text.startswith("## Instructor Courses\n")
    assert "`555`" in text and "Student" not in text


def test_student_only(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, STUDENT_ONLY)})
    assert text.startswith("## Student Courses\n")
    assert "`777`" in text and "Instructor" not in text


def test_unknown_heading_and_missing_heading_go_to_other(monkeypatch):
    body = page(
        '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
        + box("1", "A 1")
        + '</div><h2 class="pageHeading">Archived</h2>'
        + box("2", "A 2")
    )
    text, _ = run(monkeypatch, {"/account": (200, body)})
    assert text.startswith("## Other Courses\n")
    assert "`1`\n  - Semester: Fall 2026\n  - Assignments: 3 assignments\n  - Section: not stated on the page" in text
    assert "`2`\n  - Semester: N/A\n  - Assignments: 3 assignments\n  - Section: Archived" in text


def test_no_heading_with_create_button_is_instructor(monkeypatch):
    body = page('<div class="courseList">' + box("1", "A 1") + "</div>", create=True)
    text, _ = run(monkeypatch, {"/account": (200, body)})
    assert text.startswith("## Instructor Courses\n")


def test_genuinely_empty_account_is_not_an_error(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, EMPTY)})
    assert text.startswith("No courses found for this account.")
    assert "course ID can still be used directly" in text


def test_unparseable_course_links_are_an_error_not_an_empty_list(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, UNPARSEABLE)})
    assert text.startswith("Error: Could not parse the course list")
    assert "2 course link(s)" in text and "No courses found" not in text


def test_unreadable_boxes_are_an_error(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (200, UNREADABLE_BOXES)})
    assert text.startswith("Error: Could not parse the course list")
    assert "1 course box(es), 1 without a readable name" in text


def test_partly_unreadable_boxes_are_flagged(monkeypatch):
    body = page(
        '<h2 class="pageHeading">Instructor Courses</h2><div class="courseList">'
        + box("1", "A 1")
        + '<a class="courseBox" href="/courses/9"><div>?</div></a></div>'
    )
    text, _ = run(monkeypatch, {"/account": (200, body)})
    assert "`1`" in text and "1 course box(es) on the account page had no readable" in text


def test_falls_back_to_root_page_when_account_has_no_course_list(monkeypatch):
    text, session = run(
        monkeypatch,
        {"/account": (200, "<html><body>Settings</body></html>"), "/": (200, CLASSIC)},
    )
    assert session.urls == [BASE + "/account", BASE + "/"]
    assert "`111`" in text


def test_page_without_any_course_list_is_an_error(monkeypatch):
    other = "<html><body>Settings</body></html>"
    text, _ = run(monkeypatch, {"/account": (200, other), "/": (200, other)})
    assert text.startswith("Error: Could not parse the course list")
    assert "no course list section" in text


def test_root_fallback_http_failure_keeps_account_result(monkeypatch):
    other = "<html><body>Settings</body></html>"
    text, _ = run(monkeypatch, {"/account": (200, other), "/": (500, "")})
    assert "no course list section" in text


def test_http_failure_is_an_error_with_status(monkeypatch):
    text, _ = run(monkeypatch, {"/account": (503, "down")})
    assert text.startswith("Error: Unable to load the Gradescope account page /account (HTTP 503)")


def test_auth_error_is_reported(monkeypatch):
    def boom():
        raise AuthError("no credentials")

    monkeypatch.setattr(courses, "get_connection", boom)
    assert courses.list_courses() == "Authentication error: no credentials"


def test_glued_assignment_label_is_separated(monkeypatch):
    body = page(
        '<h2 class="pageHeading">Instructor Courses</h2><div class="courseList">'
        + box("1", "A 1", count='1 assignment<span>No Published Grades</span>')
        + "</div>"
    )
    text, _ = run(monkeypatch, {"/account": (200, body)})
    assert "  - Assignments: 1 assignment · No Published Grades" in text


@pytest.mark.parametrize(
    "href", ["/courses/42", "/courses/42/", "https://www.gradescope.com/courses/42"]
)
def test_course_href_forms(monkeypatch, href):
    body = page(
        '<h2 class="pageHeading">Student Courses</h2>'
        f'<a class="courseBox" href="{href}"><h3 class="courseBox--shortname">X</h3></a>'
    )
    text, _ = run(monkeypatch, {"/account": (200, body)})
    assert "- **X**\n  - ID: `42`" in text


def test_deep_course_links_are_not_course_boxes():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        page('<a class="courseBox" href="/courses/42/assignments">x</a>'), "html.parser"
    )
    parsed = courses._parse_account_courses(soup)
    assert parsed["boxes"] == 0 and parsed["course_links"] == 0
