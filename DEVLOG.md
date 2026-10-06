# Gradescope MCP — Development Log

> This file records implementation history, behavior changes, and project-level
> documentation updates. `AGENT.md` should stay aligned with the current state
> summarized here.

---

## Session 16 — 2026-10-06: Issue #13 (staff uploads on behalf of students)

### What was done

1. **`tool_inspect_submission_upload_form`** (read-only). Lists the file-upload
   forms of Manage Submissions or one submission page: method, action,
   whether the upload tool would use it, file inputs, candidate student
   fields (with the size of a student list) and the number of other fields.
2. **`tool_upload_submission_for_student`** (write, not idempotent). Reuses
   the upload path from #18: `_validate_upload_path`, `_upload_roots`,
   `expected_sha256`, read-once bytes, and `_upload_outcome` for the result,
   so success needs a redirect to a new submission page without an error
   message. The student must be a roster Student; the preview names them.
   The student's current submission comes from the scores export:
   without `submission_id` the upload creates a first submission (refused,
   naming it, if one exists); with `submission_id`, which must be the
   current one, it replaces it through the submission page. An unreadable
   scores export uploads nothing. The form is read from the live page, only
   POST forms under this assignment on this site are used, and its fields
   are sent like a browser sends them (no disabled controls, no unchecked
   boxes). Submission IDs linked from the form's page and the student's
   current one count as existing for the success check.
3. The multipart body is built by requests (`files=`) from the bytes read,
   so no `requests-toolbelt` dependency is added.

### Not verified live

Only tested against offline fakes. What still needs a sandbox course with
demo students: the real Manage Submissions and submission-page forms per
assignment type (field names, whether replacement posts to the submission
page), and where Gradescope redirects after a staff upload (a new
submission ID is required for success).

### Current state

- **41 tools** + **3 resources** + **7 prompts**
- **959 automated tests** (`uv run pytest -q`), all passing

---

## Session 15 — 2026-10-05: Issues #8 (credential refresh) and #7 (LMS gradebook CSV)

### What was done

1. **#8 — credentials fixed in `.env` apply without a restart.** `.env`
   discovery/loading moved from `__main__` to a new `envfiles` module
   (re-exported from `__main__`, behavior unchanged). `main()` loads with
   `remember_credentials=True`; `auth.get_connection()` calls
   `envfiles.refresh_credentials()` before every login attempt, which
   re-reads `GRADESCOPE_EMAIL` / `GRADESCOPE_PASSWORD` from the same files
   (same trust checks). Only variables that came from `.env` are refreshed;
   the MCP client's `env` block still wins. Changed credentials have a new
   fingerprint, so an old cooldown no longer blocks them. Credential errors
   now say where a fix takes effect.
2. **#7 — `tool_export_lms_gradebook`** (new local-cache tool, 39 tools). It
   writes a Canvas (`Student, ID, SIS User ID, SIS Login ID, Section,
   <column>` + `Points Possible` row) or Brightspace (`OrgDefinedId` /
   `Username`, `<column> Points Grade <Numeric MaxPoints:N>`,
   `End-of-Line Indicator`) import CSV from the `/scores` export into the
   private cache. Only fully graded scores are exported by default; missing
   students are blank (or 0), partial totals are opt-in, students without a
   matching key are listed, and spreadsheet formulas in names are
   neutralized. Formats follow the Canvas and Brightspace import docs;
   checked live on a demo course.

### Current state

- **39 tools** + **3 resources** + **7 prompts**
- **941 automated tests** (`uv run pytest -q`), all passing

---

## Session 14 — 2026-10-02: Fixes From a Live Read-Only Test (L1-L6)

### Why

The tools were run against a real Gradescope account, read-only (every
write tool only as a preview). Two write tools could not work on real
pages: `tool_modify_assignment_dates` refused every partial update, and
`tool_grade_answer_group` refused every group. Four smaller defects
showed up in the read tools. The fixes are tested with synthetic fixtures
that mirror the observed page structures; no write was run live.

### Dates (`assignments.py`, L1)

- The settings page has no date inputs in its server HTML (they are
  rendered client-side). The current values sit in the
  `SetupDueDateFormGroup` React props: `releaseDate`, `dueDate`,
  `hardDueDateEnabled`, `hardDueDate` (present when enabled),
  `syncLmsDueDate` and `timezone`, as course-local `YYYY-MM-DDTHH:MM`.
  `_read_date_form` now prefers those props (`hardDueDateEnabled` is the
  late-submission flag, `hardDueDate` the late due date) and keeps the
  input reader as a fallback per value. The read-back uses the same
  reader. A missing or unparseable value is still refused, never guessed.
- The write still posts gradescopeapi's form field names
  (`assignment[release_date_string]`, ...).
- Preview and result name the course timezone the page reports (e.g.
  `America/Los_Angeles (PDT)`) and warn when `syncLmsDueDate` is on, since
  an LMS sync may overwrite the due date.

### Group grading (`answer_groups.py`, L2, L3)

- Gradescope serves `/answer_groups/{g}/grade` as a 302 to the
  representative submission's `/submissions/{sid}/grade?group_mode=true`,
  whose `urls.save_grade` already ends in `/save_many_grades`. Such a URL
  is now used when the page is in group mode for this group: `group_mode`
  true, `answer_group` equal to the group, the URL in this course and
  question, its submission the page's `submission.id` and a confirmed
  member (`_group_mode_problem`). `/save_grade` URLs are still rewritten;
  anything else is refused. The page-identity checks (`_group_page_problem`)
  now run before the save URL is resolved and cover both URL forms.
- A group with no confirmed members is refused before its grade page is
  fetched (Gradescope redirects it to the `/answer_groups` overview). The
  Error lists the inferred members and points to the answer-grouping UI
  or individual grading. A grade page that lands on that overview (an
  `AnswerGrouper` page) for another reason is reported as such instead of
  "SubmissionGrader component not found".

### Read tools (L4-L6)

- `tool_list_courses` (`courses.py`): the course box's count and its
  child label were glued together by the upstream parser ("1
  assignmentNo Published Grades"); they are now shown as
  `1 assignment · No Published Grades`.
- `tool_get_assignment_outline` (`grading.py`): AssignmentEditor props
  carry no assignment type, so online assignments showed "Type: Unknown".
  They now show "Online assignment"; a reported type (e.g. PDFAssignment)
  is kept. `_get_outline_data` adds an `_outline_component` key.
- `tool_get_assignments` (`assignments.py`): gradescopeapi drops
  AssignmentsTable rows of type `assignment_container`, so a course box
  could count 8 assignments while the tool listed 7. The tool now fetches
  the page itself (same requests, gradescopeapi's parsers) and names the
  containers in a note below the table; the table and total are
  unchanged.

### Tests

- `tests/test_live_fixes.py` covers L1-L6. Run against the previous code,
  its tests reproduce the live failures ("the settings page has no
  release_date field", "unexpected save URL ... (expected a path ending in
  /save_grade)", "SubmissionGrader component not found", "Type: Unknown").
- Three existing tests that faked `conn.account.get_assignments` for
  `get_assignments` now fake `_fetch_assignment_listing`; their assertions
  are unchanged.

### Behavior changes for MCP clients

- `tool_modify_assignment_dates` works on the real settings page; preview
  and result gain a timezone and an LMS-sync line.
- `tool_grade_answer_group` accepts the group-mode page; zero-member
  groups get a clearer Error without a page request.
- `tool_list_courses`, `tool_get_assignment_outline` and
  `tool_get_assignments` output as described above.

### Current state

- **38 tools** + **3 resources** + **7 prompts**
- **918 automated tests** (`uv run pytest -q`), all passing

---

## Session 13 — 2026-10-02: Round-3 Review Fixes and Follow-ups

### Why

A third adversarial review of the Session 12 state found more ways for a
write to go beyond what the user approved. One batch overwrite approval
covered every row graded at write time, and a group overwrite was not tied
to the graded members the user saw. A rejected upload could be reported as
a success, and the approved upload content was not bound to the write.
`set_extension` hid a `visible` flip and let a `timezone` argument override
the course's. A bad `.env` or a deleted working directory stopped the server
from starting. A second session expiry was documented wrongly. A slow page
download could run far past its 120 s budget, and hidden or generic check
icons marked regrades completed. These were fixed in `2a194ab`..`ad87ec5`.
Verifying those fixes found thirteen smaller follow-ups (round 4, C1-C13),
fixed at the end of this session together with the documentation.

### Grade writes (`grading_ops.py`, `answer_groups.py`, `server.py`)

- Per-row batch overwrite (`2a194ab`): `tool_apply_grade_batch` no longer
  has a batch-wide `overwrite_graded`. A row graded at write time is written
  only if that row carries `"overwrite": true`. Otherwise it is listed under
  "Not written: already graded at write time". A client that still sends
  the batch flag gets graded rows skipped. The preview marks rows SKIPPED or
  OVERWRITTEN and says to leave SKIPPED rows out of the confirmed call.
- The batch preview refuses `"overwrite": true` on a row that is not graded.
  It now also refuses it on a row that already holds exactly the requested
  grade (C1): nothing is sent for that row, so the flag could only
  overwrite a grade entered after the preview. A retry that re-previews a
  row an earlier confirm already wrote hits this. `tool_apply_grade` warns
  when `overwrite_graded=True` has nothing to overwrite.
- A row's `overwrite` accepts only JSON `true`, `false` or `null` at the MCP
  layer (C3). Before, pydantic's lax mode turned `"yes"`, `"1"`, `1` and
  `1.0` into true.
- Group overwrite (`5afa5ea`): `tool_grade_answer_group` takes
  `expected_graded_ids`, the graded member IDs its preview prints. With
  `confirm_write=True` and graded members, the list is required. The call
  is refused, with nothing sent, unless exactly those members are graded.
  An identical repeat of a call that went through is refused this way,
  since that call graded the rest of the group. The message now says so
  (C2).

### Uploads (`submissions.py`)

- `57f7f79`: success needs the upload POST's own redirect to a new
  submission of this assignment, not one seen on the assignment page
  before. The final page must be that submission and show no error.
  `expected_sha256` binds the upload to the content the user approved.
- Only a visible, error-styled flash message (`alert-danger`,
  `flash-error`, ...) counts as an error (C4). Hidden elements, JS
  templates and `<noscript>` content are ignored. A warning or unstyled
  `role="alert"` on the new submission's page is quoted under a ⚠️ line of
  the success result. A `❌` result quotes the error message on its own
  line.

### Extensions (`extensions.py`)

- `fcff048`: a stored `visible` other than true is reported as changing.
  A `timezone` that differs from the course timezone Gradescope reports is
  an Error.
- When the page reports several timezones, or one this server can't load,
  a `timezone` argument is refused as well, and the Error names what was
  reported (C5). It no longer passes with a note claiming Gradescope
  reports no timezone. The refusal tells the user to omit `timezone` or
  pass the course's (C6); it no longer offers "give the dates with a UTC
  offset" as a fix on its own.
- When the argument stood in for an unreported zone and the read-back after
  the write reports a different one, the result is a ⚠️ warning naming both
  zones and the dates in course time, not ✅ (C7).

### Start-up and session recovery (`__main__.py`, `auth.py`, `server.py`)

- `2ddeb48`: an unreadable `.env` or a deleted working directory is skipped
  with a logged reason instead of stopping the server.
- `eb62fd0` and C12: README, AGENT, `with_session_recovery` and the
  `gs_tool` docstring state both results of a second expiry. A re-run that
  had a write accepted returns its output plus the notice. Otherwise the
  result is the recovery error followed by an earlier output.

### Page downloads (`grading_workflow.py`, `auth.py`, `pyproject.toml`)

- `8130bf1`: the 120 s deadline bounds the whole download. Bodies are read
  with urllib3's `read1`, and a watchdog shuts down a read still blocked at
  the deadline.
- `urllib3>=2.3` is declared (`read1` is from 2.2, `shutdown()` from 2.3)
  (C8). With an older urllib3 the reader falls back to `iter_content`
  instead of failing every page.
- The session-expiry hook no longer reads a streamed body (C10, C13). For a
  same-site HTML answer it leaves the logged-out-page check to the reader
  (`auth.check_streamed_body`). The reader runs that check after its
  bounded read, so the deadline and the 25 MB cap now also hold for a
  Gradescope-hosted URL answered as HTML. A JPEG labelled `text/html` is
  again recognized. A body another hook already read is taken from
  `resp.content`.

### Regrades (`regrades.py`, `server.py`)

- `ad87ec5`: only visible, specific evidence is ✅. That means a checked
  checkbox, a date, a status word or label, or an icon-library check
  mark. Hidden or greyed-out icons and a generic `check` class are ❓.
- A checkbox whose state contradicts the cell's visible text or labels is
  ❓ (C9). Examples are a checked box next to "Pending" and an unchecked
  one next to "Completed". Before, the box alone decided, so the first
  example was ✅. The `tool_get_regrade_requests` description now states
  these rules (C11).

### Known limitations (documented, not fixed)

- The write cannot know which rows a preview marked SKIPPED. A SKIPPED row
  left in the confirmed call is written if its grade is cleared in the
  meantime. The preview, docstrings and skill say to leave such rows out.
- An approved overwrite (`"overwrite": true`, `overwrite_graded=True`)
  replaces whatever grade the submission holds at write time, even one
  changed after the preview. The result names the grade it overwrote. A
  per-row expected-state check would close this.

### Corrections to earlier entries

- Session 12 (R2-0) says `tool_apply_grade_batch` takes
  `overwrite_graded`. That is superseded by the per-row `"overwrite": true`
  above.
- Session 12 (R2-9) says a second expiry returns the recovery error
  followed by the first attempt's output. That holds only when the re-run
  had no write accepted.
- Session 12 (R2-14) says a check-mark icon is ✅. Now only a visible
  icon-library check mark counts, and conflicting evidence is ❓.

### Behavior changes for MCP clients

- `tool_apply_grade_batch`: `overwrite_graded` is gone (ignored). There is
  a per-row `overwrite` (strict boolean), and the preview refuses it on
  ungraded rows and rows already holding the grade.
- `tool_grade_answer_group`: `expected_graded_ids` is required to confirm
  over graded members.
- `tool_upload_submission`: optional `expected_sha256`. More `❌ Upload not
  confirmed` outcomes, and fewer for messages that are not errors.
- `tool_set_extension`: new timezone Errors, a ⚠️ result when the read-back
  reveals another zone, and a `visible` flip line.
- `tool_get_regrade_requests`: more rows are ❓ instead of ✅ or ⏳.
- `manage_extensions_workflow` and the stand-in timezone warning now advise
  UTC offsets (not a timezone argument) when the page reports several zones
  or one this server can't load.

### Current state

- **38 tools** + **3 resources** + **7 prompts**
- **876 automated tests** (`uv run pytest -q`), all passing

---

## Session 12 — 2026-10-02: Round-2 Review Fixes

### Why

A second adversarial review of the Session 11 state (round-2 findings
R2-0..R2-34) found incomplete fixes and new problems. Confirmed writes could
still overwrite a grade entered after the preview, or land on another
submission's page. Untrusted blocks could be forged. A session-recovery
re-run could misreport writes that had already been saved. Login failures
were cached until a restart, or never cached at all. The MCP schema took
booleans as numbers and non-ASCII digits as IDs. Fixed in `c551665`..`a35f730`
plus this documentation update. Where correct behavior depends on Gradescope
behavior that can't be observed offline, the code again takes the
conservative option.

### Grade writes (`grading_ops.py`, `answer_groups.py`)

- Overwrite protection (R2-0): `tool_apply_grade` and
  `tool_apply_grade_batch` take `overwrite_graded` (default false). Each
  submission is re-read right before its write. One that is graded by then,
  including one graded by another grader after the preview, is refused
  (`apply_grade`) or listed under "Not written: already graded at write
  time" (batch). With the opt-in, the result names every grade it overwrote.
  A graded submission that already holds exactly the requested grade is
  reported and not re-sent.
- Write-target checks (R2-7): the grade goes to the save URL of the page
  Gradescope serves. `apply_grade` and both batch phases refuse a save URL
  for another course, question or submission. `tool_grade_answer_group`
  refuses a redirect to another group's page, a page of another
  `answer_group`, and a save URL outside the question or through another
  group's confirmed member.
- Batch cap (R2-12): at most 50 rows (`MAX_BATCH_ROWS`) per call, refused
  before any request. mcp does not stop a running tool when the client
  cancels, so the cap also bounds what a timed-out batch can still write.
- Unknown scoring type (R2-18): a missing `scoring_type` is reported as
  unknown by the rubric, the grading context (JSON `scoring_type: null` plus
  `scoring_type_note`) and the regrade detail, instead of defaulting to
  `negative`. Previews warn that their projection assumes deduction.
- The preview footer now reads "Show this preview to the user; only after
  they explicitly approve, re-run with `confirm_write=True` ..." (R2-21).

### Untrusted text (`common.py`)

- Unforgeable blocks (R2-6, R2-17, R2-23): BEGIN and END markers carry a
  random per-call block id. `<<<` / `>>>` runs and runs of three or more
  backticks in the text are broken up with zero-width spaces, so a student
  can neither close the fence nor forge an END line.
- Student display names and roster emails are kept on one line
  (`common.sanitize_inline`) in the grading context, the `apply_grade`
  preview, smart-read and the submission headings (R2-10).

### Sessions and login (`auth.py`, `__main__.py`)

- The logged-out home page (login form, no logout link, e.g. after a
  redirect to `/`) is an expiry signal (R2-1). Before, writes that landed
  there reported success and reads returned empty results.
- Session recovery after committed writes (R2-5): the response hook counts
  the writes Gradescope accepted during the call. After an accepted write
  the call is not re-run, since a re-run could repeat it or report a
  finished delete or group grade as "nothing changed". The result gets a
  notice to verify with the read tools instead. A second expiry returns the
  recovery error followed by the first attempt's output, no longer replacing
  it (R2-9).
- Login cooldowns (R2-8): invalid credentials wait 10 minutes; HTTP
  429/5xx honour `Retry-After` (1 minute without one, at most 15); a "too
  many attempts" page waits 5 minutes; other rejections ("login rejected
  (HTTP <status>)") wait 1 minute. Before, any rejection was cached as
  invalid credentials until a restart, and 429/5xx were retried on every
  call. Every message states the remaining wait.
- `.env` loading (R2-24): from the working directory, then from the source
  checkout, never from parent directories. A file owned by another user or
  writable by everyone is skipped, and the files loaded or skipped are
  logged. Start-up side effects moved from import time into `main()`.

### Dates, extensions, uploads (`assignments.py`, `extensions.py`, `submissions.py`)

- Extension settings preservation (R2-3): `tool_set_extension` sends the
  student's whole current extension with the requested dates replaced, so
  other dates and time limits survive. The read-back reports any setting
  Gradescope dropped or changed. Preview and write refuse when the
  extensions page can't be read.
- Timezones (R2-2, R2-29): `tool_get_extensions` showed UTC-stored values
  labelled as course-local time, and an agent re-sending them moved the
  deadline. It now shows `local time = UTC instant` plus other settings.
  Assignment listings keep the UTC offset of aware dates.
- Per-assignment write locks (R2-4): confirmed date writes per assignment
  and extension writes per student run one at a time in the process (a
  300 s wait, then an Error), so two approved calls can no longer revert
  each other.
- Both date previews return the authentication error instead of a
  misleading preview when the login fails (R2-13). Requested values that
  were already set are labelled as such, not "(unchanged)" (R2-26).
- `tool_get_assignment_details` reports an unknown assignment as an Error
  (R2-28).
- Upload confirmation (R2-11): success only when Gradescope opens the new
  submission's page; anything else is "❌ Upload not confirmed" with the
  final page.
- `tool_get_assignment_submissions` no longer reads scores from a guessed
  column; graded status it can't read is shown as unknown (R2-15).

### Read side and workflow (`regrades.py`, `grading_workflow.py`)

- A regrade completion cell with an unlabelled icon is ❓ unknown, and a
  check-mark icon is ✅ (R2-14). Regrade detail uses the shared crop-page
  and rubric rules (R2-27).
- Assignment auto-resolution skips unreadable (401, non-JSON) assignments
  and stops after 3 non-JSON pages in a row or an auth error (R2-16).
- Crop page numbers are normalized (`"5"`, `5.0` and `5` are one page;
  R2-30). Page downloads are streamed with a hard 25 MB cap and a 120 s
  budget per page (R2-31). The artifact's and smart-read's confidence bands
  come from the `grading_ops` constants (R2-22).

### Interface (`server.py`)

- Stricter ID and number validation (R2-19, R2-20, R2-25, R2-33). IDs are
  ASCII digits only (`^[0-9]+$`) and lose leading zeros, so `"031"` and
  `"31"` are one batch row. Number arguments reject `true` / `false`;
  before, `point_adjustment: true` wrote +1 and `confidence: true` skipped
  the review flag.
- `grades` advertises `maxItems: 50`. Tool descriptions and prompts follow
  the fixes above: `overwrite_graded` only after explicit approval, block
  ids, unknown scoring, the extension merge, unknown graded status. The
  claim that subagents cannot call write tools in the Claude Code harness
  was removed (R2-34); the server only says subagents should propose rows.
- Annotations are unchanged: `idempotentHint` follows MCP's "no additional
  effect" definition, so repeated deletes and group grades stay idempotent
  although the repeat reports differently.

### Documentation (this commit)

- README, AGENT, the skill and `.env.example` describe all of the above.
  The skill no longer tells the agent to drop weight-0 leaf questions
  (R2-32), sends it to the user when the scoring type is unknown, and adds
  the `overwrite_graded` approval step to single, batch and regrade writes.
- `tests/test_docs_consistency.py` now also checks the documented ID
  pattern, number arguments, batch cap, write-lock wait, page-download
  limits, login cooldowns and untrusted-block markers against the code, and
  that superseded claims do not return.

### Corrections to earlier entries

- Session 11 says a rejected login is cached until the credentials change,
  that IDs are digit strings, and that the batch preview flags graded rows
  that would be OVERWRITTEN. These are superseded by the cooldowns, the
  ASCII-only IDs and the `overwrite_graded` opt-in above.

### Behavior changes for MCP clients

- Writing over a graded submission with `tool_apply_grade` or
  `tool_apply_grade_batch` needs `overwrite_graded=True`.
- Schema errors for `true` / `false` in number arguments and non-ASCII
  digits in IDs; leading zeros are dropped; batches over 50 rows return an
  Error.
- New `isError` results: an unknown assignment in
  `tool_get_assignment_details`, and `❌ Upload not confirmed`.
- Untrusted blocks differ between calls (random block id).
- Login errors end with "Not trying to log in again for <wait>."; a write
  result may end with a notice that the session expired after accepted
  writes.

### Current state

- **38 tools** + **3 resources** + **7 prompts**
- **717 automated tests** (`uv run pytest -q`), all passing

---

## Session 11 — 2026-10-02: Hardening Pass (Write Safety, Sessions, Interface, Docs)

### Why

A multi-agent code review of the Session 10 state (findings V1-V4) found
writes that could do more than their previews showed, missing session-expiry
handling, a credential leak path, a shared world-readable cache, and an MCP
surface and docs that had drifted from the code. This session fixes them in
focused commits (`d9d81ca`..`bd8cede`) plus this documentation update. Where
correct behavior depends on Gradescope behavior that can't be observed
offline, the code takes the conservative option and says so.

### Grade-write validation (`grading_ops.py`, `answer_groups.py`)

- Unknown or stale rubric item IDs are refused before anything is sent
  (V1-3). Previously the write went ahead with every listed item silently
  dropped and all other items sent unchecked; `tool_apply_grade_batch` wrote
  such rows with a warning. Integer and backticked IDs are normalized (V1-2).
- `tool_apply_grade` previews the student, current score, the items it will
  CHECK and UNCHECK, the resolved adjustment and comment, and the projected
  score; the result reports the score read back from Gradescope (V1-8).
- The documented confidence tiers now exist in code (V1-4): below 0.6
  nothing is written; 0.6 to 0.8 inclusive is written but flagged NEEDS
  HUMAN REVIEW; NaN/inf are rejected.
- `tool_apply_grade_batch` (V1-9): unknown keys, duplicate submission IDs
  and malformed numbers refuse the batch; the preview loads every row and
  flags rows that would be OVERWRITTEN; execution re-reads each row right
  before saving and reads it back afterwards.
- Rubric CRUD (V1-5): negative weights need `allow_negative=True`; previews
  state the ADD/DEDUCT effect; create warns about duplicates and treats a
  non-JSON success as an unknown result; update/delete verify the item
  exists and read the rubric back.
- `tool_grade_answer_group` (V1-6, V1-7): everything is validated before the
  preview; already-graded members need `overwrite_graded=True`;
  `expected_member_count` aborts the write if membership changed; the
  preview lists checked and unchecked items and the projected score; the
  POST no longer follows redirects and needs a JSON 2xx to count as saved.

### Dates and timezones (`assignments.py`, `extensions.py`)

- Date inputs need an explicit time (`YYYY-MM-DDTHH:MM`); bare dates,
  non-zero seconds and impossible dates are rejected; `""` means unset.
- `tool_modify_assignment_dates` used to send empty values for omitted
  dates and `allow_late_submissions=0` unless a late due date was given,
  while its preview showed only the supplied dates (V1-1). It now merges the
  current values from the assignment settings, keeps the late-submission flag
  unless `late_due_date` is given, refuses when a value it must keep is
  unreadable, and verifies by re-reading the settings.
- `tool_set_extension` interpreted naive dates in the server host's timezone
  (V2-2). Naive dates now use the course timezone from the extensions page
  or the new `timezone` argument; offset dates are absolute; the order is
  checked; the preview shows the UTC instants and the current extension,
  and the write is read back.

### Read-side correctness

- Grading progress follows the outline numbering and no longer falls back to
  another assignment's data (V3-5); outline subparts render (nested rows).
- Student lookups match email case-insensitively; scores CSV responses that
  are HTML are rejected; the export summary reports what its statistics are
  based on (V3-6, V3-8). `tool_get_student_assignment_link` accepts
  `student_email` (V3-7).
- Statistics no longer delete the overall table and tolerate undefined
  values (V3-9, V3-10).
- Regrade completion needs positive evidence; unreadable rows are ❓ unknown;
  regrade detail shows the current score, adjustment, comment and scoring
  direction (V3-11, V3-12).
- Graders are read from the grader column by header and described as who
  last graded (V3-13); extension 401 handling no longer matches IDs
  containing "401" (V3-14); the submissions fallback table is read by
  header, and roster rows that can't be parsed are reported instead of
  dropped.
- `tool_list_question_submissions` reports `graded: null` when unknown
  (V3-2); `tool_get_next_ungraded` stays within the question, wraps around,
  and returns an Error instead of a false "all graded" (V3-1);
  `tool_get_student_submission_map` keys students by email (V3-3); the
  grading context lists crop pages ±1 instead of the first few pages and
  reports `page_count` (V3-4).
- Student-authored text (typed answers, regrade messages, answer-group
  titles and inferred answers) is returned in `<<<BEGIN UNTRUSTED ...>>>`
  blocks or flagged in JSON (V2-8). Answer-group counts are consistent
  between listing and detail (V3-16).
- Uploads (V2-7): absolute regular files up to 100 MB; hidden and
  credential-like files and system directories refused; optional
  `GRADESCOPE_MCP_UPLOAD_ROOT`; the preview shows SHA-256.

### Workflow and readiness (`grading_workflow.py`)

- Readiness is described and computed as pre-read context, not grading
  confidence (V4-1). It now uses per-submission signals (student work
  located, placeholder pages, crop pages missing from the submission), and
  "no student work" is capped at `not_ready`.
- All workflow tools collect and score the same pages, so they report the
  same readiness for a submission (V4-2).
- The grading artifact includes `scoring_type`, `floor`, `ceiling` and
  signed rubric effects (V4-6), and a rubric summary is labelled "not a
  reference answer" (V4-3). Outline failures are reported as unknown
  instead of "expected for scanned PDFs" (V4-5).
- Assignment resolution falls back when a given `assignment_id` is wrong or
  inaccessible and memoizes per process (V4-4).
- `tool_cache_relevant_pages` caches all pages by default in the
  implementation too, checks image bytes, names unnumbered pages uniquely,
  lists failed pages while keeping the rest, and no longer sends the CSRF
  header to third-party image hosts (V4-7).
- The answer key skips group headers, keeps weight-0 leaves and sorts by
  label (V4-8); smart-read merges Tiers 1-2 (same page image), lists every
  other page and shows typed answers (V4-9).

### Cache hardening (`cache.py`)

- The shared `/tmp/gradescope-mcp` root (created with default permissions,
  adoptable by another user, symlink-following writes) is replaced by a
  private per-user root: `GRADESCOPE_MCP_CACHE_DIR`, else
  `$XDG_RUNTIME_DIR/gradescope-mcp`, else `<tempdir>/gradescope-mcp-<uid>`
  (V2-4). Directories are 0700, files 0600, written atomically through
  `O_EXCL | O_NOFOLLOW` temp files; unsafe roots are refused.
- `tests/conftest.py` gives every test its own cache and no credentials
  (V4-12).

### Authentication and session recovery (`auth.py`, `server.py`)

- gradescopeapi's login sent the credentials as URL query parameters, and a
  connect failure could carry that URL (with the password) into tool output
  (V2-1). The server now logs in itself with a form body and scrubs
  credentials from every message.
- A rejected login is cached: later calls fail fast without contacting
  Gradescope until the credentials change.
- Every request has a default timeout (10 s connect, 60 s read;
  `GRADESCOPE_MCP_HTTP_TIMEOUT`) through `TimeoutHTTPAdapter` (V2-6).
- Session expiry is detected by a response hook (`SessionExpiredError`)
  before redirects are followed, and every tool and resource re-runs once
  on a fresh login (V2-3). The unused, flawed opt-in helpers
  `with_session_retry`, `request_with_retry` and
  `is_session_expired_response` were removed.

### Interface, annotations and prompts (`server.py`, `tools/common.py`)

- All 38 tools carry complete `ToolAnnotations` and a title; the 11
  `confirm_write` tools are `destructiveHint`, 3 local-cache tools are
  neither read-only nor destructive.
- IDs are validated as digit strings at the schema layer (numbers accepted)
  before any URL is built (V2-5). Batch rows are a strict `GradeRow` type;
  `output_format` and `filter` are enums; the workflow tools require
  `question_id` / `submission_id`.
- Handled failures (`Error`, `Authentication error`, `❌`) are returned with
  `isError: true`; resources raise JSON-RPC errors; tools return text only
  (no duplicated `structuredContent`) (V2-9).
- New wrapper parameters: `output_format` on the grading context,
  `allow_negative`, `timezone`, `overwrite_graded`, `expected_member_count`
  and `student_email`.
- All 7 prompts were rewritten so none reaches a write without a preview and
  explicit approval; `auto_grade_question` is an approval-gated batch flow
  (V4-10).
- The crop-page selection rule is shared in `tools/common.py` (V4-13).

### Documentation (this commit)

- `README.md`, `AGENT.md` and the skill were checked against the code:
  complete tool inventory with annotation kinds, configuration and
  environment variables, write safety, `isError`, dates and timezones,
  untrusted text, cache location, authentication; the skill now uses
  `tool_apply_grade_batch`, the answer-group guards, the confidence tiers
  as implemented, and paths printed by the tools.
- The skill installs into a client skills directory (for Claude Code
  `~/.claude/skills/`), not `/tmp`. `OPERATIONS_LOGS/` is documented as a
  local, untracked log (it was referenced but never shipped).
- `.env.example` lists the optional variables; the root-anchored
  `/tmp/gradescope-*` line, which could never match the system `/tmp`, was
  removed from `.gitignore`.
- `tests/test_docs_consistency.py` checks the stated counts, inventories,
  prompt and resource tables, tool and parameter names used in the docs,
  documented environment variables, the confidence thresholds and the
  project tree against the code. README and AGENT no longer state a test
  count; it is recorded here.

### Corrections to earlier entries

- Session 7 item 4 said `prepare_grading_artifact` and
  `assess_submission_readiness` produce identical readiness scores. They fed
  different page lists into the score until this session.
- Sessions 8-10 describe `/tmp/gradescope-mcp` as the cache root; see Cache
  hardening above.
- The May 2026 commits (`924e1ab`, `2b4d052`, `e18c629`, `1e9743b`) were not
  logged: they added `tool_apply_grade_batch`,
  `tool_get_student_submission_map`, `tool_get_student_assignment_link`, the
  JSON score export, fixes surfaced by an earlier review, and the opt-in
  auth retry helpers removed above.

### Behavior changes for MCP clients

- Failures now arrive with `isError: true`; schema errors read
  `Error executing tool <name>: ... validation error ...`.
- Numeric IDs are accepted; non-numeric IDs, unknown batch-row keys and
  values outside an enum are rejected before the tool runs.
- `tool_grade_answer_group` requires `rubric_item_ids`; the four workflow
  tools take `question_id` / `submission_id` as required arguments.
- Results no longer include `structuredContent`.

### Current state

- **38 tools** + **3 resources** + **7 prompts**
- **482 automated tests** (`uv run pytest -q`), all passing

---

## Session 10 — 2026-09-29: Upgrade To MCP Python SDK v2 And gradescopeapi 1.8.1

### What was done

1. Migrated from `mcp` 1.26 (`FastMCP`) to `mcp` 2.2 (`MCPServer`). `mcp` 2.x
   removed `mcp.server.fastmcp`, and the old unbounded `mcp>=1.26.0` pin let
   non-lockfile installs resolve 2.x and crash at import.
2. `server.py` now builds `MCPServer("Gradescope MCP Server", version=...)`;
   v2 reports an empty `serverInfo.version` unless one is passed.
3. `auth.py` guards singleton login/reset with a lock. v2 runs sync tool
   functions on worker threads (v1 ran them inline on the event loop), so
   concurrent first calls previously could all log in.
4. Bumped `gradescopeapi` to 1.8.1. Its only change is the upstream roster
   submissions-column fix; this project uses its own `_parse_roster`, which
   was checked against the roster fixture shipped in the 1.8.1 wheel.
5. Pinned `mcp>=2.2.0,<3` and `gradescopeapi>=1.8.1`.
6. Audited the full gradescopeapi 1.8.1 surface (identical to upstream `main`)
   against this server. The one released capability not yet exposed was
   `update_autograder_image_name` (added upstream in 1.6.0), now wrapped as
   the preview-first write tool `tool_update_autograder_image`.
   `remove_student_extension` is still `NotImplementedError` upstream, and
   create-assignment / edit-outline / submission-download exist only as open
   upstream PRs, so nothing else was adopted.

### Behavior changes

- Tool calls no longer block the event loop, so pings and cancellation are
  processed while a tool waits on Gradescope, and independent tool calls can
  run concurrently (anyio's default worker-thread limit).
- Tool results, error signalling (`isError`), argument validation, resources
  and prompts are unchanged on the wire; verified over stdio with protocol
  versions `2025-06-18` and `2025-11-25`.

### New tests added

- `tests/test_server_mcp.py`: registration counts, worker-thread execution,
  write preview and argument validation through the `MCPServer` layer.
- `tests/test_auth.py`: concurrent first `get_connection()` calls log in once.
- `tests/test_assignments_and_grading_ops.py`: `update_autograder_image`
  preview makes no requests, validation, upstream call, rejection and HTTP
  error reporting.

### Current state

- **38 tools** + **3 resources** + **7 prompts**
- **77 automated tests**

---

## Session 9 — 2026-03-18: Full Project Audit And Documentation Refresh

### What was done

1. Read through the full repository structure, server registration layer, core
   tool modules, workflow helpers, tests, and operator-facing docs.
2. Reconciled the documented project state with the actual code in
   `src/gradescope_mcp/server.py`.
3. Updated all primary human-readable project files so they reflect the current
   implementation instead of older snapshots.

### Documentation fixes

- `README.md`
  - Rewritten around the current architecture and real feature set
  - Tool inventory corrected and reorganized by workflow
  - Current counts corrected to **34 tools**, **3 resources**, **7 prompts**
  - Added architecture, constraints, ID semantics, scoring semantics, and
    scanned-assignment notes
  - Added current test count: **30 automated tests**

- `AGENT.md`
  - Rewritten as an accurate maintainer/developer guide
  - Corrected tool counts, test counts, and module responsibilities
  - Added explicit maintenance rule to keep docs in sync with server changes

- `OPERATIONS_LOGS/RECORDS.md`
  - Converted from a minimal placeholder into a clearer mutation-log template
  - Added logging rules for sensitive data handling and rollback expectations
  - *Note (2026-10-02): `OPERATIONS_LOGS/` is gitignored, so this file was
    only ever local and is not in the repository.*

- `.env.example`
  - Clarified that `.env` is loaded automatically by the module entry point
  - Kept the credential surface minimal

- `pyproject.toml`
  - Updated the package description to better match the actual server scope

### Audit findings

- The server currently registers **34** `@mcp.tool()` functions.
- The repository currently contains **30** test functions across 5 test files.
- Earlier docs still mentioned **32** or **33** tools and older test totals.
- The codebase's main operational constraints are:
  - preview-first writes with `confirm_write=True` gating
  - Global Submission ID vs Question Submission ID distinction
  - positive vs negative scoring semantics with always-positive rubric weights
  - `/tmp/gradescope-mcp` as ephemeral cache root for grading artifacts and answer-key material

### Current state

- **34 tools** + **3 resources** + **7 prompts**
- **30 automated tests**
- Core docs synchronized with the implementation as of 2026-03-18

---

## Session 8 — 2026-03-18: JSON Payload Fix, Scoring Defaults, Parallel Grading Tool

### What was done

#### Bug fixes
1. **`apply_grade` / `grade_answer_group` JSON payload** (`grading_ops.py`, `answer_groups.py`):
   - Gradescope's frontend sends `Content-Type: application/json` with `{"rubric_items": {"ID": {"score": "true"}}, "question_submission_evaluation": {...}}`.
   - Old code sent form-encoded `data=` with keys like `rubric_item_ids[ID]=true`, which returned 500.
   - Fix: switched from `data=payload` to `json=payload` with the correct nested structure.

2. **`scoring_type` default was wrong** (`grading_ops.py`):
   - Default was `"positive"` (additive), but Gradescope defaults to `"negative"` (deduction: correct = 0, mistakes = negative weight).
   - Fix: changed fallback in 3 locations from `"positive"` to `"negative"`.

3. **Added scoring direction hints** (`grading_ops.py`):
   - `get_submission_grading_context` now shows: `Rubric items **add** points` (positive) or `Starts at full marks. Rubric items **deduct** points for errors.` (negative).
   - `get_question_rubric` also shows the scoring direction.
   - Prevents agents from using the wrong rubric sign convention.

#### New tool
4. **`list_question_submissions`** (`grading_ops.py`, `server.py`):
   - Scrapes all Question Submission IDs from `/questions/{qid}/submissions`.
   - Supports `filter` param: `"all"`, `"ungraded"`, `"graded"`.
   - Returns JSON with `submission_id`, `student_name`, `graded` status.
   - **Why**: `get_assignment_submissions` returns Global Submission IDs (404 with grading tools). `get_next_ungraded` has race conditions under parallel use. This tool enables the main agent to pre-allocate specific Question Submission IDs to subagents.

#### Skill updates
5. **SKILL.md** (`skills/gradescope-assisted-grading/SKILL.md`):
   - Added parallel grading best practices: one question per subagent, ID pre-allocation via `tool_list_question_submissions`.
   - Added Global ID vs Question ID distinction warning.
   - Added JSON payload debugging hint to safety rules.
   - Previously graded submission skip-by-default policy.
   - `/tmp/gradescope-mcp` file persistence warning for cross-conversation sessions.

#### Docstring corrections
6. **`create_rubric_item` / `tool_create_rubric_item`** — updated weight semantics per scoring type (positive = adds points, negative = deducts points).

### New tests added
- `test_apply_grade_sends_json_payload` — verifies `json=` kwarg with correct nested structure
- `test_positive_scoring_context_shows_add_hint` — verifies positive scoring shows "add points" hint

### Test results
- **20 automated tests** at the time of this session — all passing
- 5 test files

### Files modified
| File | Changes |
|------|---------|
| `tools/grading_ops.py` | JSON payload, scoring_type default, direction hints, `list_question_submissions`, rubric weight convention fix |
| `tools/answer_groups.py` | JSON payload for `grade_answer_group` |
| `server.py` | Import + register `tool_list_question_submissions`, docstring fixes |
| `skills/.../SKILL.md` | Parallel grading policy, ID pre-allocation, batch approval, cross-agent consensus, scoring auto-detection, visual cross-validation, direct grading links, rubric weight guidance, safety rules |
| `tests/test_assignments_and_grading_ops.py` | 2 new tests |

#### Rubric weight convention fix
7. **Rubric weights are always positive** (`grading_ops.py`, `server.py`, SKILL.md):
   - Gradescope stores weights as positive numbers regardless of scoring type.
   - `scoring_type` determines interpretation: positive = earned points, negative = deducted points.
   - A deduction item with `weight=2.0` means "student loses 2 points" — the web UI shows `-2`.
   - Fixed docstrings in `create_rubric_item` and `tool_create_rubric_item` that previously told agents to pass negative values.
   - Updated scoring hints in `get_submission_grading_context` to clarify the positive-weight convention.

#### SKILL design improvements
8. **Batch approval** (SKILL.md):
   - For 50+ submissions, agents collect previews into a summary table (student, score, rubric items, confidence, link).
   - User approves in bulk: "全部通过" / "除了 #3" / "#3 改成 7 分".
   - Batch size: 10–30 per approval round.

9. **Cross-agent consensus** (SKILL.md):
   - Main agent deduplicates rubric gap reports from parallel subagents.
   - N ≥ 2 same-gap reports → one rubric proposal, not N alerts.
   - Subagent return format: `gap_description`, `affected_submission_ids`, `suggested_rubric_change`.

10. **Scoring mode auto-detection** (SKILL.md):
    - Mandatory step before grading: read `scoring_type` from grading context.
    - Stop if rubric weights conflict with stated scoring type.

11. **Visual cross-validation** (SKILL.md):
    - For numerical answers, compare crop vs full-page reading.
    - OCR disagreement → force confidence < 0.6 → flag for human review.

12. **Direct grading links** (SKILL.md):
    - Skipped submissions include clickable Gradescope link: `https://www.gradescope.com/courses/{cid}/questions/{qid}/submissions/{sid}/grade`
    - Post-grading report includes links for all skipped submissions.

### Current state
- **33 tools** + **3 resources** + **7 prompts**
- 20 automated tests (all passing at that point)

---

## Session 7 — 2026-03-18: Bug Fix Sprint (10 fixes across 7 files)

### What was done

Systematic code review identified 12+ potential bugs; 10 were confirmed as real issues and fixed.

#### Critical fixes
1. **`get_next_ungraded` self-loop** (`grading_ops.py`):
   - Gradescope's `next_ungraded` URL points to the *current* submission when it's itself ungraded.
   - Old behavior: returned the same submission the caller was already on.
   - Fix: detects the self-loop, advances via `next_submission`, checks if the next one is ungraded, and returns it. If it's graded, follows *its* `next_ungraded`.

2. **`get_submission_grading_context` self-referencing nav** (`grading_ops.py`):
   - `previous_ungraded`/`next_ungraded` nav entries that point to the current submission are now filtered out to avoid misleading agents.

3. **`prepare_grading_artifact` fabricates "reference answer available"** (`grading_workflow.py`):
   - Rubric-drafted fallback text was passed to `_compute_readiness()` as a real reference answer, inflating the score by +0.2.
   - Fix: only real `explanation` goes into readiness scoring. The rubric draft is labeled "Rubric-Based Fallback" in the artifact.

4. **Readiness score inconsistency** (`grading_workflow.py`):
   - Same root cause as item 3. Both `prepare_grading_artifact` and `assess_submission_readiness` now produce identical scores.
   - *Correction (2026-10-02): they still scored different page lists; see Session 11.*

#### High-priority fixes
5. **`get_assignment_outline` missing question IDs** (`grading.py`):
   - Standalone questions (no children) now output `**Question ID:** \`{id}\`` so downstream tools can find them.

6. **Unclear 404 error for wrong submission ID type** (`grading_ops.py`):
   - 404 error now includes a contextual hint: "This often means you are using a Global Submission ID instead of a Question Submission ID."

7. **`apply_grade` / `grade_answer_group` rubric_item_ids coercion** (`grading_ops.py`, `answer_groups.py`):
   - MCP clients sometimes pass a single string `"123"` instead of `["123"]`. Both functions now auto-wrap strings into lists.

#### Medium / low-priority fixes
8. **`get_answer_groups` markdown "Type: (not set)"** (`answer_groups.py`):
   - Falls back to per-group `question_type` when `assisted_grading_type` is None. Added Type column to the table.

9. **`get_assignment_graders` leaking internal IDs** (`submissions.py`):
   - No longer lists the filtered entries' internal IDs/labels. Only reports the count.

10. **`extensions.py` 401 for exam-type assignments** (`extensions.py`):
    - Catches 401 errors and returns a friendly message explaining that some assignment types don't support the extensions API.

11. **Reference answer UX** (`grading_workflow.py`):
    - All 3 "no reference answer" messages now explain this is expected for scanned PDF / handwritten assignments, not an extraction failure.

### Test results
- **18 automated tests** — all passing at the time of this session
- 5 test files

### Files modified
| File | Changes |
|------|---------|
| `tools/grading_ops.py` | Self-loop fix, 404 hint, rubric_item_ids coercion |
| `tools/grading_workflow.py` | Readiness fix, reference answer labeling, UX messages |
| `tools/grading.py` | Standalone question ID output |
| `tools/answer_groups.py` | Type column, rubric_item_ids coercion |
| `tools/submissions.py` | Grader list sanitization |
| `tools/extensions.py` | 401 error handling |
| `tests/test_extensions_and_answer_key.py` | Updated assertions for new messages |

### Current state
- **32 tools** + **3 resources** + **7 prompts**
- 18 automated tests (all passing at that point)

---

## Session 6 — 2026-03-17: Answer Groups, Rubric CRUD, JSON Output

### What was done
1. **Answer Groups** — 3 new tools in `tools/answer_groups.py`:
   - `get_answer_groups(course_id, question_id)` → lists all AI-clustered answer groups with sizes
   - `get_answer_group_detail(course_id, question_id, group_id)` → shows members, crops, graded status
   - `grade_answer_group(course_id, question_id, group_id, ...)` → batch-grades via `save_many_grades`
   - Both markdown and JSON output supported

2. **Rubric CRUD** — 2 new tools in `tools/grading_ops.py`:
   - `update_rubric_item(...)` → modify description/weight (cascades to all submissions)
   - `delete_rubric_item(...)` → remove item (cascades to all submissions)
   - Both have `confirm_write` gates with cascade warnings

3. **JSON Output Mode**:
   - `get_submission_grading_context` now accepts `output_format="json"` for structured data
   - Returns parsed rubric items, navigation, answer_group, progress, pages, crops
   - `get_answer_groups` and `get_answer_group_detail` also support JSON mode

### Key API discoveries
- `/courses/{cid}/questions/{qid}/answer_groups` → full JSON with all groups + submissions
- Group grading uses `save_many_grades` endpoint (not `save_grade`)
- Rubric items support PUT (update) and DELETE on `/rubric_items/{item_id}`
- SubmissionGrader props contain answer group metadata: `answer_group`, `answer_group_size`, `groups_present`

### Test results
| Tool | Test Data | Result |
|------|-----------|--------|
| `get_answer_groups` (markdown) | Q5a (midterm) | OK |
| `get_answer_groups` (JSON) | Q5a | OK |
| `get_submission_grading_context` (JSON) | Q5a sub | OK |
| `update_rubric_item` (dry run) | Q5a | OK |
| `delete_rubric_item` (dry run) | Q5a | OK |
| `grade_answer_group` (dry run) | mocked | OK |

### Current state
- **32 tools** + **3 resources** + **7 prompts**
- 10 automated tests (all passing at that point)

---

## Session 5 — 2026-03-17: Safety Rails, Tests, and Agent Hardening

### What was done
1. Added a two-step confirmation gate for write-capable tools:
   - `tool_upload_submission(..., confirm_write=False)`
   - `tool_set_extension(..., confirm_write=False)`
   - `tool_modify_assignment_dates(..., confirm_write=False)`
   - `tool_rename_assignment(..., confirm_write=False)`
   - `tool_apply_grade(..., confirm_write=False)`
   - `tool_create_rubric_item(..., confirm_write=False)`
2. Fixed upload path validation:
   - uploads now require absolute paths
3. Hardened scanned-page caching:
   - `cache_relevant_pages()` now downloads page images through the authenticated Gradescope session instead of a separate unauthenticated stack
4. Added the first automated test suite
5. Synced project docs at that time

### Why this matters for agents
- Default-deny writes are a better fit for MCP clients, where LLMs may call
  tools speculatively.
- The server now behaves as:
  1. preview intended mutation
  2. execute only with `confirm_write=True`

### Remaining gaps noted at that time
- No structured JSON mode yet for some high-volume read tools
