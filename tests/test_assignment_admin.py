import json
from types import SimpleNamespace

from gradescope_mcp.tools import assignment_admin as admin


def test_settings_preserve_fields_and_bind_snapshot(monkeypatch):
    calls = []
    state = {
        "assignment[title]": "Exam",
        "assignment[template_visible_to_students]": "0",
        "_method": "patch",
    }

    def settings(*a):
        return (
            "<unused>",
            dict(state),
            {"assignment[template_visible_to_students]": ["1"]},
        )

    monkeypatch.setattr(admin, "_settings", settings)
    monkeypatch.setattr(admin, "_token", lambda s: "csrf")

    def post(url, **kw):
        calls.append(kw)
        state.update({k: v for k, v in kw["data"].items() if k != "authenticity_token"})
        return SimpleNamespace(status_code=302)

    monkeypatch.setattr(
        admin,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=SimpleNamespace(post=post),
        ),
    )
    p = json.loads(
        admin.prepare_assignment_plan("1", "settings", {"title": "Updated"}, "2")
    )
    assert not calls
    result = json.loads(
        admin.execute_assignment_plan(p["operation_token"], p["request_digest"], True)
    )
    assert (
        result["status"] == "verified"
        and state["assignment[template_visible_to_students]"] == "0"
    )
    assert admin.execute_assignment_plan(
        p["operation_token"], p["request_digest"], True
    ).startswith("Error:")
    assert len(calls) == 1


def test_create_requires_template_and_student_dates(monkeypatch):
    monkeypatch.setattr(admin, "get_connection", lambda: object())
    assert "require template_path" in admin.prepare_assignment_plan(
        "1", "create", {"kind": "exam", "title": "Exam"}
    )


def test_form_checkbox_and_tokens():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<form><input name="authenticity_token" value="SECRET"><input name="x" value="0" type="hidden"><input name="x" value="1" type="checkbox" checked><input name="y" value="bad" disabled></form>',
        "html.parser",
    )
    fields, choices = admin._form(soup.form)
    assert fields == {"x": "1"} and choices == {"x": ["0", "1"]}


def test_bad_form_action_is_refused():
    import pytest

    c = SimpleNamespace(gradescope_base_url="https://example.com")
    with pytest.raises(ValueError):
        admin._bound(
            c,
            "https://attacker.example/courses/1/assignments",
            "/courses/1/assignments",
        )


def test_checkbox_can_be_disabled_from_checked_state(monkeypatch):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<meta name="csrf-token" content="t"><form id="assignment_form" action="/courses/1/assignments/2"><input name="assignment[ceiling]" value="0" type="hidden"><input name="assignment[ceiling]" value="1" type="checkbox" checked></form>',
        "html.parser",
    )
    conn = SimpleNamespace(gradescope_base_url="https://example.com")
    monkeypatch.setattr(admin, "_page", lambda *a: soup)
    request, token = admin._prepare(
        conn, "1", "2", "settings", {"ceiling": False}, None
    )
    assert request["fields"]["assignment[ceiling]"] == "0" and token == "t"


def test_empty_csrf_refused():
    from bs4 import BeautifulSoup
    import pytest

    with pytest.raises(ValueError, match="CSRF"):
        admin._token(BeautifulSoup('<meta name="csrf-token">', "html.parser"))


def test_template_verification_compares_bytes(monkeypatch):
    import hashlib
    from bs4 import BeautifulSoup
    from gradescope_mcp.tools import submission_files

    soup = BeautifulSoup(
        '<a href="https://production-gradescope-uploads.s3.amazonaws.com/template.pdf">Download</a>',
        "html.parser",
    )
    monkeypatch.setattr(submission_files, "_download", lambda *a: b"%PDF-original")
    admin._verify_template(object(), soup, hashlib.sha256(b"%PDF-original").hexdigest())
    import pytest

    with pytest.raises(ValueError, match="differ"):
        admin._verify_template(
            object(), soup, hashlib.sha256(b"%PDF-other").hexdigest()
        )


def test_date_wire_format_is_canonical():
    assert (
        admin._values({"due_date_string": "2027-01-02 12:30"})[
            "assignment[due_date_string]"
        ]
        == "2027-01-02T12:30"
    )


def test_internal_clone_template_does_not_relax_public_upload_policy(monkeypatch):
    from gradescope_mcp.cache import get_artifact_path, write_artifact
    import pytest
    path=write_artifact(get_artifact_path('clone-template-'+'a'*20+'.pdf'),b'%PDF-test')
    def forbidden(*a):raise ValueError('public upload refused')
    monkeypatch.setattr(admin,'_validate_upload_path',forbidden)
    monkeypatch.setattr(admin,'_upload_roots',lambda:None)
    with pytest.raises(ValueError,match='public upload'):admin._file(str(path))
    assert admin._file(str(path),True)['size']==9
    monkeypatch.setattr(admin,'_upload_roots',lambda:['/explicit/root'])
    with pytest.raises(ValueError,match='public upload'):admin._file(str(path),True)


def test_internal_clone_template_executes_without_public_path_bypass(monkeypatch):
    from gradescope_mcp import operations
    from gradescope_mcp.cache import get_artifact_path, write_artifact
    from bs4 import BeautifulSoup
    path=write_artifact(get_artifact_path('clone-template-'+'b'*20+'.pdf'),b'%PDF-test')
    monkeypatch.setattr(admin,'_upload_roots',lambda:None)
    meta=admin._file(str(path),True)
    fields={'assignment[type]':'PDFAssignment','assignment[title]':'Copy'}
    fresh={'target':'/courses/1/assignments','fields':fields,'file':meta,'baseline':{},'verify':fields}
    request={**fresh,'course_id':'1','assignment_id':None,'action':'create','options':{'kind':'exam','title':'Copy'},'template_path':str(path),'trusted_template':True}
    op=operations.create('assignment',request,{},[])
    sent=[]
    def refused(*a):raise ValueError('public file policy refuses runtime directories')
    monkeypatch.setattr(admin,'_validate_upload_path',refused)
    monkeypatch.setattr(admin,'_prepare',lambda *a:(fresh,'csrf'))
    monkeypatch.setattr(admin,'_settings',lambda *a:(BeautifulSoup('<html></html>','html.parser'),fields,{}))
    monkeypatch.setattr(admin,'_verify_template',lambda *a:None)
    conn=SimpleNamespace(gradescope_base_url='https://example.com',session=SimpleNamespace(post=lambda *a,**k:(sent.append(k) or SimpleNamespace(status_code=302,headers={'Location':'/courses/1/assignments/9/outline/edit'}))))
    monkeypatch.setattr(admin,'get_connection',lambda:conn)
    result=json.loads(admin.execute_assignment_plan(op['operation_token'],op['request_digest'],True))
    assert result['status']=='verified' and sent[0]['files']['template_pdf'][1]==b'%PDF-test'
