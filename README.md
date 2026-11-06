# Gradescope MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for
[Gradescope](https://www.gradescope.com/) that exposes course management,
grading, regrade review, statistics, and AI-assisted grading workflows to MCP
clients.

The server is designed for instructors and TAs who want to use AI agents with
real Gradescope data while keeping every write behind a preview and an
explicit confirmation step.

This repository also includes a reusable skill at
`skills/gradescope-assisted-grading/SKILL.md` for human-approved grading
workflows.


## Architecture and capability audit

The core is **inspect → read evidence → plan → preview → write → verify**.
The public MCP registry in `server.py` is the source of truth; the existing tool
names remain compatible. The layers have distinct responsibilities:

| Layer | Responsibility | Implementation |
| --- | --- | --- |
| MCP boundary | ID/type validation, annotations, error mapping, session recovery | `server.py` |
| Connection | Login, credential refresh, HTTP deadlines, accepted-write tracking | `auth.py`, `envfiles.py` |
| Shared data and writes | Outline editor parsing, per-object serialization, previews | `tools/outline_data.py`, `tools/safety.py` |
| Domain operations | Courses, assignments, extensions, submissions, grading, rubric, groups, regrades | `tools/` |
| Workflow | Answer key, evidence cache, readiness, grading artifacts | `tools/grading_workflow.py`, `cache.py` |
| Guidance | Prompts and assisted-grading skill; these do not add backend APIs | MCP prompts, `skills/gradescope-assisted-grading/SKILL.md` |

Use `uv run python -m gradescope_mcp.inspect` for an offline JSON inventory of
installed versions, tool schemas, write classifications, resources and prompts.
It reads the actual MCP registry and does not load `.env` or contact Gradescope.
This avoids maintaining a second executable capability list.

### Which tools to use

- Discovery: courses → assignments → outline; use the roster only when needed.
- Grading: resolve question-submission IDs → smart read/context → rubric →
  single/batch grade preview → approved write → progress/scores.
- Assignment administration: dates, extensions, title, autograder image, outline.
- Reporting: scores and statistics; LMS export produces an import file, not an
  LMS synchronization request.
- Workflow helpers are optional conveniences. Smart read is the normal entry;
  readiness, page caching and full artifacts are lower-level choices, not four
  mandatory steps for every submission. Prompts describe workflows; they do not
  repeat the implementation of grading tools.

### Lifecycle coverage and boundaries

| Stage | Available | Boundary |
| --- | --- | --- |
| Assignment setup | Creation plans, editable settings, PDF template upload, composed assignment copy | Copy excludes student data, LMS links, autograder images and rubric group constraints |
| Outline/rubric | Partial question patches, PDF crop overlays, identification regions, flat rubric export/import/copy | No automatic OCR-based region approval; review rendered pages |
| Submissions | Scoped attachment/page downloads, ZIP, PDF count checks, reproducible sampling | Typed-only submissions may have no attachment; 25 MiB/file and 250 MiB/batch caps |
| Grading | Local evidence-bound plans, calibration, snapshot checks, per-row journal, cancellation/resume | Cancellation does not undo in-flight writes; uncertain outcomes stop for review |
| Grouping | Create, confirm, move/remove members, merge groups | Graded/unknown-state members are refused; no destructive reset |
| Review | Progress, missing submissions, totals, bonus configuration, evidence/score archives | Mechanical checks are not proof of grading correctness |
| Publication | Separate publish/unpublish and notification plans | Mail delivery is not proven by submission success |
| Regrades/accommodations | Staff response/completion request, local override deletion/time limits | Reopening must be confirmed by backend read-back; inherited overrides are not edited here |
| Autograder | Discovered configuration/build status/logs, ZIP plan, rerun form plan, result reads | No accessible programming assignment was available for live validation; a missing/ambiguous form is refused |
| LMS | CSV exports plus posting to an already linked LMS resource and job polling | Does not create institutional LTI/SSO deployments or synchronize rosters |
| Authentication | Native credentials and recovery | Interactive institutional SSO is not implemented |

Start with assignment configuration, then prepare local plans, show their exact
requests, obtain explicit approval and execute. Operation tokens and request
digests bind execution to the reviewed plan. The operation journal distinguishes
prepared, sending, verified/completed, accepted_unverified, unknown, stale,
needs_review and cancelled states. Never interpret HTTP acceptance as completed
mail delivery, a finished autograder run or a successful LMS job. Query journals
and the corresponding read tool before retrying. No universal rollback is claimed.

Plan preparation returns a new local operation each time and is marked
non-idempotent. Keep and reuse its token instead of preparing duplicate operations.
A grade plan can store per-submission evidence references; unknown outcomes are
reconciled against current grades and never blindly resent. The old direct grading
tools remain compatible; planned execution is preferred for long agent workflows.

Question write locks coordinate threads and MCP processes sharing the same private
cache root. They cannot coordinate a web user or another cache root. These writes
now require an accessible private cache even when their result is returned as text.
Archives record outline, rubric and scoring fingerprints; freshness checks do not
claim that historical exported scores are current. Workflow answer-key markdown
remains an explicit cache; use versioned archives/plans for source-bound grading.

### Upstream audit on 2026-11-06

- [gradescopeapi 1.8.1](https://pypi.org/project/gradescopeapi/) is still the latest
  published version. The [upstream repository](https://github.com/nyuoss/gradescope-api)
  main branch matched tag v1.8.1 when checked. No newer outline/grading API is
  available there. Its `remove_student_extension` is still a `NotImplementedError`,
  despite the README advertising extension removal.
- [MCP SDK 2.3.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
  replaces 2.2.0. The lockfile has been refreshed to compatible stable releases;
  Python 3.10 support is retained by the resolver. Gradescope's browser endpoints
  remain separate from the unofficial Python library's optional REST wrapper.
- No new first-party general REST API was identified in the official material
  checked. [Gradescope's supported LMS integrations](https://www.gradescope.com/help)
  are not a drop-in replacement for session-based instructor operations.

### Editing outlines

Call `tool_update_assignment_outline` without `outline_json` to read the full
editable tree and revision. Preserve existing IDs and fields. Submit the edited
list as `outline_json` to preview, then pass `expected_revision` and
`confirm_write=True` only after explicit approval of that payload.

PDF additions omit `id`; existing nodes retain `crop_rect_list`. Crop coordinates
are percentages with one-based page numbers, for example
`{"x1":10,"y1":10,"x2":90,"y2":30,"page_number":1}`. Name/ID regions are preserved.
Online additions use unique **negative integer IDs** (-1, -2, ...), type
`OnlineQuestion` or `QuestionGroup`, one-based `index`, and the enclosing group's
`parent_id` (null at the top). Content blocks include
`{"type":"text","value":"Question text"}` and
`{"type":"explanation","value":"Reference answer"}`. Preserve other input blocks.
Read-back verifies existing IDs as well as content. After a warning, inspect the
outline before retrying. PDF revisions detect changes since the preview but the
server offers no verified atomic precondition; external edits during a request
can still race. No real assignment was mutated during this audit.

## Current Status

- 74 MCP tools (34 read-only, 25 that write to Gradescope, 15 that only write
  to the private local cache)
- 3 MCP resources (1 static resource, 2 URI templates)
- 7 MCP prompts
- Python 3.10+
- MCP Python SDK v2 (`mcp>=2.3,<3`, `MCPServer`) and `gradescopeapi>=1.8.1`
- Package manager: `uv`
- Offline test suite: `uv run pytest -q` (the current count is recorded in
  `DEVLOG.md`)

## What The Project Provides

### Read-oriented workflows
- Course discovery and assignment listing
- Assignment outline parsing for online and scanned-PDF assignments
- Roster inspection with a custom HTML parser
- Submission listing for multiple assignment types
- Grading progress, rubric context, answer groups, regrades, and statistics
- Workflow helpers that write grading artifacts, answer-key snapshots and
  page images to a private per-user cache (see [Local cache](#local-cache);
  the tools print the real path)

### Write-oriented workflows
- Uploading submissions
- Setting student extensions
- Modifying assignment dates
- Renaming assignments
- Changing a programming assignment's autograder Docker image
- Applying grades, one at a time or in batches
- Creating, updating, and deleting rubric items
- Batch grading answer groups

Every tool that writes to Gradescope returns a preview unless it is called
with `confirm_write=True`. See [Write safety](#write-safety).

## Tool Inventory

The Kind column matches the tool annotations clients see: `read-only`
(`readOnlyHint`), `write` (changes Gradescope data; `destructiveHint`, has
`confirm_write`), or `local cache` (reads Gradescope, writes files only to
the private local cache).

### Courses And Assignments
| Tool | Kind | Description |
|------|------|-------------|
| `tool_list_courses` | read-only | List all courses grouped by role (instructor vs student) |
| `tool_get_assignments` | read-only | List a course's assignments with dates; status and grade for student accounts (N/A for staff) |
| `tool_get_assignment_details` | read-only | One assignment's name and dates (status and grade for student accounts) |
| `tool_upload_submission` | write | Upload local files as a new submission from the logged-in account (path restrictions and success check below) |

### Instructor / TA Management
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_course_roster` | read-only | Roster grouped by role: name, email, SID, user ID, submission count, sections |
| `tool_get_extensions` | read-only | Extensions for one assignment: dates as course-local time = UTC instant, plus other settings (e.g. time limit) |
| `tool_set_extension` | write | Add or update one student's extension; other current settings are re-sent and kept; `timezone` for dates without an offset |
| `tool_modify_assignment_dates` | write | Change release / due / late-due dates; omitted dates are kept |
| `tool_rename_assignment` | write | Rename an assignment |
| `tool_update_autograder_image` | write | Change a programming assignment's autograder Docker Hub image |
| `tool_get_assignment_submissions` | read-only | Global Submission IDs with graded status, progress and late flag |
| `tool_get_student_submission` | read-only | One student's typed answers (untrusted blocks), file and page links, per-question and total scores |
| `tool_get_assignment_graders` | read-only | Staff who last graded a question's submissions, with counts (not the assigned graders) |

### Grading Read
| Tool | Kind | Description |
|------|------|-------------|
| `tool_prepare_grade_plan` | local cache | Snapshot-bound dry-run grades and exact payloads |
| `tool_execute_grade_plan` | write | Resumable approved grading with per-row journal |
| `tool_get_operation` | read-only | Inspect operation status and uncertain outcomes |
| `tool_cancel_operation` | local cache | Cooperatively stop future operation rows |
| `tool_calibrate_grade_plan` | read-only | Compare proposed scores to human references |
| `tool_get_assignment_admin` | read-only | Settings, publication and form capabilities |
| `tool_prepare_assignment_plan` | local cache | Plan creation, configuration, template, publication, mail or linked LMS posting |
| `tool_execute_assignment_plan` | write | Execute exact approved assignment plan once |
| `tool_export_question_rubric` | read-only | Flat rubric export with source revision |
| `tool_prepare_rubric_plan` | local cache | Prepare rubric import/copy/update/delete plan |
| `tool_execute_rubric_plan` | write | Verified rubric changes with partial journal |
| `tool_patch_assignment_outline` | write | Modify selected question fields preserving the rest |
| `tool_preview_outline_regions` | local cache | Render PDF crop overlays |
| `tool_inspect_submission_pdf` | read-only | Check PDF page counts and required pages |
| `tool_download_submission_files` | local cache | Validated original attachments, page images and batch ZIP |
| `tool_respond_regrade_request` | write | Staff reply and verified completion/reopen request |
| `tool_modify_extension_options` | write | Edit time limits or delete local overrides |
| `tool_set_answer_group_members` | write | Confirm/move/remove ungraded group members |
| `tool_review_assignment` | read-only | Progress, missing work, score/bonus anomalies and publishing state |
| `tool_sample_question_submissions` | read-only | Reproducible sample for human/agent calibration |
| `tool_archive_assignment` | local cache | Private grading evidence/score ZIP with fingerprints |
| `tool_check_archive_freshness` | read-only | Detect stale outline/rubric archives |
| `tool_prepare_choice_bonus_plan` | local cache | Class-wide A/B/C tally and reviewed bonus plan |
| `tool_prepare_assignment_clone` | local cache | Prepare copy of settings/template/outline/flat rubrics |
| `tool_execute_assignment_clone` | write | Execute copy with partial journal and created assignment ID |
| `tool_get_autograder_status` | read-only | Configuration choices and discovered build/log state |
| `tool_prepare_autograder_plan` | local cache | Plan supported configuration or discovered rerun form |
| `tool_execute_autograder_plan` | write | Submit approved build/rerun once; completion separate |
| `tool_get_autograder_results` | read-only | Read available programming submission test results |
| `tool_poll_operation` | local cache | Poll and record linked LMS grade-posting jobs |
| `tool_merge_answer_groups` | write | Merge ungraded groups with membership verification |
| `tool_set_question_scoring` | write | Positive/negative scoring, floor/ceiling; preserves zero weight |
| `tool_set_question_grading_method` | write | Individual/manual or configured AI grouping; optional all assignment questions |
| `tool_create_answer_group` | write | Create a manual group and confirm ungraded, ungrouped submissions |
| `tool_update_assignment_outline` | write | Read editable JSON; preview and write PDF/online question trees, points, crops and content; requires revision |
| `tool_get_assignment_outline` | read-only | Question hierarchy (nested subparts), IDs, types, weights, prompt text; no rubric items |
| `tool_export_assignment_scores` | read-only | Score export summary; `output_format="json"` returns every student with per-question scores |
| `tool_get_grading_progress` | read-only | Per-question graded counts, numbered as in the outline |
| `tool_get_submission_grading_context` | read-only | Rubric (IDs, applied state), score, typed answer, crop pages ±1; `output_format="json"` available |
| `tool_get_question_rubric` | read-only | Rubric items and scoring type without a submission ID |
| `tool_list_question_submissions` | read-only | Question Submission IDs; `filter` = `all` / `ungraded` / `graded` (`graded: null` rows, state unknown, appear only under `all`) |
| `tool_get_student_submission_map` | read-only | Per-student map of question ID to Question Submission ID, keyed by email |
| `tool_get_student_assignment_link` | read-only | Per-student whole-assignment submission URL, by name or `student_email` |
| `tool_get_next_ungraded` | read-only | Grading context of the next ungraded submission of the same question |

### Grading Write
| Tool | Kind | Description |
|------|------|-------------|
| `tool_apply_grade` | write | Set one submission's rubric items, point adjustment and comment; an already graded submission needs `overwrite_graded`; reads the score back |
| `tool_apply_grade_batch` | write | Grade up to 50 submissions of one question in one call; typed rows, a graded row skipped unless that row has `overwrite: true`, per-row read-back |
| `tool_create_rubric_item` | write | Create a rubric item (positive weight; ADD/DEDUCT shown in the preview) |
| `tool_update_rubric_item` | write | Update an existing rubric item's description or weight |
| `tool_delete_rubric_item` | write | Delete a rubric item (removes it from every submission) |

### AI-Assisted / Workflow Helpers
| Tool | Kind | Description |
|------|------|-------------|
| `tool_prepare_grading_artifact` | local cache | Write a question's grading artifact (prompt, scoring type, signed rubric, reference answer or rubric summary, crops, sample pages) and print its path |
| `tool_assess_submission_readiness` | read-only | Report pre-read context (prompt, reference, rubric, crop, located student work); not grading confidence |
| `tool_cache_relevant_pages` | local cache | Download a submission's page images (all pages by default; `include_all_pages=False` for crop pages and neighbours) and print the directory |
| `tool_prepare_answer_key` | local cache | Write an assignment-wide grading basis (prompts, instructor reference answers) and print its path |
| `tool_export_lms_gradebook` | local cache | Write the assignment's scores as a Canvas or Brightspace gradebook import CSV (fully graded scores only by default) and print its path |
| `tool_smart_read_submission` | read-only | Crop-first reading plan: crop page, adjacent pages, other pages; typed answers for online questions |

### Answer Groups
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_answer_groups` | read-only | AI-clustered answer groups with sizes, graded counts and inferred members |
| `tool_get_answer_group_detail` | read-only | One group's members, crops and graded counts (confirmed and inferred) |
| `tool_grade_answer_group` | write | Grade every member of one group; `overwrite_graded` + `expected_graded_ids` and `expected_member_count` guards |

### Regrades
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_regrade_requests` | read-only | Regrade requests with status ✅ completed / ⏳ pending / ❓ unknown |
| `tool_get_regrade_detail` | read-only | Current score, adjustment, comment, rubric, pages, staff response and the student's message |

### Statistics
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_assignment_statistics` | read-only | Assignment summary and per-question averages; flags low-scoring graded questions |

## Resources

| URI | Description |
|-----|-------------|
| `gradescope://courses` | Current course list |
| `gradescope://courses/{course_id}/assignments` | Assignment list for a course |
| `gradescope://courses/{course_id}/roster` | Roster for a course |

`course_id` in a resource URI must be ASCII digits (leading zeros are
dropped, as for tool IDs); anything else is a JSON-RPC error (`-32602`,
"Invalid resource URI ..."). A resource whose read fails
(for example missing credentials) returns a JSON-RPC error carrying the
message instead of content.

## Prompts

| Prompt | Description |
|--------|-------------|
| `summarize_course_progress` | Summarize assignment status for a student account, or submission and grading progress for staff |
| `manage_extensions_workflow` | View extensions, then preview and (after approval) set new ones |
| `check_submission_stats` | Submission, missing, late and graded counts plus dates for one assignment |
| `generate_rubric_from_outline` | Propose a rubric per question; nothing is created without previews and approval |
| `grade_submission_with_rubric` | Grade one student's questions; grades are written only after previews and approval |
| `review_regrade_requests` | Review open (⏳) and unknown-status (❓) regrade requests; changes only after previews and approval |
| `auto_grade_question` | Assisted grading for one question: read, propose, preview a batch, get approval, write, verify |

Despite its name, `auto_grade_question` does not grade on its own: it tells
the agent to preview each batch (at most 50 rows) with
`tool_apply_grade_batch(confirm_write=False)` and to write only the rows the
user explicitly approves. An already graded row is written only if the user
approves overwriting that row's grade: the row gets `"overwrite": true` and
the batch is previewed again.
`review_regrade_requests` likewise previews each approved change with
`tool_apply_grade(overwrite_graded=True, confirm_write=False)` (a regraded
submission is already graded) and repeats that call with
`confirm_write=True` only after the user approves the preview.

## How The Tools Behave

### IDs
- Every Gradescope ID parameter (`course_id`, `assignment_id`,
  `question_id`, `submission_id`, `group_id`, `rubric_item_id`, `user_id`,
  `rubric_item_ids` and `expected_graded_ids` elements, and batch-row IDs)
  is a string of ASCII digits (`^[0-9]+$` in the input schema).
- JSON numbers are accepted and converted; surrounding whitespace and
  backticks are stripped, and leading zeros are dropped (`"031"` is the
  same ID as `"31"`, `"000"` is `"0"`). Anything else (`"../1"`, `"-1"`,
  `1.5`, `true`, non-ASCII digits such as `"１２３"`) is rejected before the
  tool runs.
- An optional ID that is blank or `null` means "not given".

### Numbers

`offset` and `limit` page operation journals (at most 100 rows per read).

`value` sets time-limit values; `count` and `seed` control sampling; `points_a`, `points_b`, `points_c` set minority-game bonuses.

`required_pages` lists one-based PDF pages to check.

`tolerance` controls calibration error tolerance.
- The number arguments (`point_adjustment`, `confidence`, `weight`,
  `expected_member_count`, and `point_adjustment` / `confidence` in batch
  rows) accept JSON numbers and numeric strings. `true` and `false` are
  rejected (`must be a number, not a boolean`) instead of being read as 1
  and 0. `expected_member_count` must be a whole number of at least 0.
- A batch row's `overwrite` accepts only JSON `true`, `false` or `null`.
  Strings and numbers (`"yes"`, `"true"`, `1`) are rejected (`must be
  true, false or null`) instead of opting the row into an overwrite.

### Submission IDs
- `tool_get_assignment_submissions` returns assignment-level Global
  Submission IDs.
- Grading tools require Question Submission IDs. Get them from
  `tool_list_question_submissions`, `tool_get_student_submission_map`,
  `tool_get_next_ungraded`, or the regrade tools. A 404 from a grading tool
  usually means a Global Submission ID was passed.
- When `submissions.json` is unavailable, `tool_get_assignment_submissions`
  reads the review-grades page. If that table has no recognizable Score or
  Graded column, graded status is reported as unknown (`?` rows,
  `Graded: unknown` or `at least N/M (K unknown)`, with a ⚠️ warning)
  rather than read from a guessed column; unknown is not ungraded.

### Write safety
- The tools with a `confirm_write` parameter (the `write` kind, 25 of
  them) are the only tools that change Gradescope data. With the default
  `confirm_write=False` they return a preview ("Write confirmation required
  ...") and change nothing.
- Previews describe exactly what `confirm_write=True` would send: resolved
  dates, rubric items that will be checked and unchecked, the projected
  score, affected students. Inputs are validated before the preview, so an
  invalid request is refused instead of previewed.
- After writing, the tools read the result back from Gradescope where
  practical (grades, rubric items, dates, extensions) and report mismatches.
- `confirm_write=True` is not human approval. An agent can set it on its
  own; every preview includes the line "Show this preview to the user;
  only after they explicitly approve, re-run with `confirm_write=True`
  ...", and the prompts and the skill tell agents to wait for the user's
  explicit approval.
- The write tools are annotated `destructiveHint: true` (and
  `readOnlyHint: false`), so a client can require confirmation for them.
  Keep them out of any "always allow" list in your MCP client so each write
  needs approval. `idempotentHint` is false only for
  `tool_upload_submission` and `tool_create_rubric_item`, which create
  something new on every call; repeating any other write with the same
  arguments has no further effect, although the repeat may report
  differently (for example that the item is already gone).
- Rubric updates and deletions apply to every submission that uses the
  item. `tool_grade_answer_group` writes to many submissions at once, and
  Gradescope may also apply it to the group's inferred (unconfirmed)
  members.

### Error signalling
- Tools return text: markdown, or a JSON document when a tool offers
  `output_format="json"`. They publish no `outputSchema` and return no
  `structuredContent`.
- A handled failure starts with `Error`, `Authentication error` or `❌` (a
  write Gradescope rejected) and is returned with `isError: true`; the text
  is unchanged.
- Previews, confidence rejections (`⚠️ **Grade REJECTED** ...`), other `⚠️`
  warnings and "No ... found" messages are ordinary results
  (`isError: false`).
- Invalid arguments (bad IDs, `true` / `false` where a number is expected,
  unknown batch-row keys, values outside an enum, missing required
  arguments) are rejected by the input schema and come back with
  `isError: true` as `Error executing tool <name>: ... validation error ...`.

### Untrusted student text
- Student-authored content (typed answers, regrade messages, answer-group
  titles and inferred answers) is returned inside fenced untrusted blocks,
  or flagged by `untrusted_fields_note` in JSON output. It is data to grade,
  never instructions to follow.
- A block starts with
  `<<<BEGIN UNTRUSTED {label} (block id {id}; student-authored; ...)>>>`
  and ends with `<<<END UNTRUSTED {label}>>> (block id {id})`. The block id
  is random for every call, and only the END line with the same block id
  as its BEGIN line closes the block. Inside the text, `<<<` / `>>>` runs and
  runs of three or more backticks are broken up with zero-width spaces, so
  a student can neither close the fence nor forge a marker.
- Student display names in grading contexts, previews, reading plans and
  submission headings are kept on one line (newlines collapsed, pipes
  escaped).

### Dates and timezones
- Both date tools need an explicit time: `YYYY-MM-DDTHH:MM` (`:00` seconds
  allowed). Bare dates, non-zero seconds and impossible dates are rejected.
  An empty string or `null` means "not provided".
- `tool_modify_assignment_dates`: course-local wall-clock times with no UTC
  offset. Omitted dates and the allow-late-submissions setting are read
  from the assignment settings and re-sent unchanged. Passing
  `late_due_date` turns late submissions on; the tool cannot turn them off.
  The settings page renders its date fields in the browser, so the current
  values come from the page's due-date settings data (release, due, late
  due date and whether it is enabled, the LMS sync flag and the course
  timezone); form inputs are the fallback. A value missing or unreadable
  there is never guessed: the call is an `Error` that asks for that date
  explicitly. The preview lists all values that will be sent, so it
  returns an error (`Authentication error` / `Error`) rather than a
  partial preview when they can't be read; the write refuses for the same
  reason. Preview and result name the course timezone the page reports
  (the IANA name, for example `America/Los_Angeles`; no DST abbreviation,
  since the one Gradescope shows is only correct for today) and warn when the due date is synced from an LMS,
  since a later sync may overwrite it. The result is verified by
  re-reading the settings the same way, and requested values that were
  already set are labelled as such.
- `tool_get_assignments` / `tool_get_assignment_details` show dates that
  Gradescope reports with a UTC offset with that offset (for example
  `2026-10-01 23:59 UTC-07:00`). `tool_get_assignments` names assignment
  containers on the staff page in a note below the table; they are not
  assignments and are not counted, so the total can be lower than the
  count `tool_list_courses` shows.
- `tool_get_extensions` names the course timezone above the table and shows
  each date as course-local time and the UTC instant Gradescope stores
  (`2026-10-01 23:59 PDT = 2026-10-02T06:59:00Z`), plus an Other Settings
  column (for example `time_limit=135, visible=true`). Either form of a
  date can be passed to `tool_set_extension`. When the course timezone is
  unknown, the line says so and dates are shown as stored.
- `tool_set_extension`: dates without an offset are wall-clock times in the
  course timezone Gradescope reports on the extensions page, never the
  server's timezone. The `timezone` argument (an IANA name) is needed only
  when Gradescope reports no course timezone; if it is given and differs
  from the timezone Gradescope reports, the call is an `Error` naming both
  and nothing is sent. When the page reports several timezones, or one the
  server doesn't know, a `timezone` argument can't be checked and is
  refused too (the `Error` names what was reported); give the dates with a
  UTC offset then. Dates with an offset (`Z`, `-07:00`) are absolute.
  Don't mix the two styles. When `timezone` stood in for an unreported
  course timezone and the read-back after the write reports a different
  one, the result is a ⚠️ warning that dates without an offset were
  resolved in the wrong zone. Dates must be in order (release <= due <= late
  due), including dates kept from the current extension.
- `tool_set_extension` sends the student's whole extension: the requested
  dates replace the current ones, every other current setting (other
  dates, a time limit, ...) is re-sent unchanged, and `visible=true` is
  always sent. Existing dates don't need to be passed again. The preview
  lists every current and outgoing setting with each date's resolved UTC
  instant; preview and write return an error when the extensions page or
  the login is unavailable. When the current extension has `visible=false`,
  preview and result list `visible: false → true` instead of saying that
  the write changes nothing. After writing, the extension is read back and
  any setting Gradescope removed or changed is reported (⚠️).
- Confirmed date changes to one assignment, and confirmed extension changes
  for one student, run one at a time within the server process. A call
  that waits more than 300 s for another one returns an Error and changes
  nothing.

### Grades and confidence
- `rubric_item_ids` is the exact set of items to check; every other item is
  unchecked. In `tool_apply_grade` and batch rows, `null` keeps the current
  rubric state and `[]` clears it. In `tool_grade_answer_group` the list is
  required.
- Every rubric item ID must be in the question's live rubric; otherwise
  nothing is sent (`tool_apply_grade_batch` refuses the whole batch).
- `confidence` (optional): below 0.6 the grade is not written; 0.6 to 0.8
  inclusive it is written but flagged NEEDS HUMAN REVIEW; above 0.8 it is
  normal. NaN and infinite values are rejected.
- Already graded submissions are not overwritten by default, and an
  overwrite approval covers only the grades the user saw.
  `tool_apply_grade` refuses one (`Error: submission ... is already
  graded`) unless `overwrite_graded=True`. `tool_apply_grade_batch` has no
  batch-wide flag: a graded row is skipped unless that row has
  `"overwrite": true`, and the preview refuses `"overwrite": true` on a row
  that is not graded or already holds exactly the requested grade (there
  it could only overwrite a grade entered after the preview). Set either
  only after the user approved overwriting those grades. The graded state
  is re-read when the write runs, so a grade entered after the preview
  (for example by another grader) is protected too: the batch lists such
  rows under "Not written: already graded at write time", whatever the
  other rows carry. The result names every grade it overwrote. A graded submission that already holds exactly the
  requested grade is reported as such, and nothing is sent.
- The grade is posted to the save URL of the grading page Gradescope
  serves. If that page belongs to another submission, nothing is sent:
  `tool_apply_grade` returns an Error, the batch preview refuses the batch,
  and at write time the row fails.
- `tool_apply_grade_batch` takes at most 50 rows per call (`maxItems` in
  the input schema); a larger batch is refused before any request, so split
  it. Rows accept only `submission_id` (required and unique; `"031"` and
  `"31"` are the same row), `rubric_item_ids`, `point_adjustment`,
  `comment`, `confidence` and `overwrite`. The preview loads every row and
  marks already graded rows as SKIPPED (or OVERWRITTEN when the row has
  `"overwrite": true`); execution re-reads each row before saving it and
  reports the scores read back from Gradescope. Leave rows the preview
  marked SKIPPED out of the confirmed call: execution cannot know what the
  preview showed, so such a row is written if its grade was cleared in
  the meantime.

### Answer groups
- `tool_grade_answer_group` refuses (with an Error listing them) when any
  confirmed or inferred member is already graded, unless
  `overwrite_graded=True`. Set that only with the user's approval. Its
  preview then prints `expected_graded_ids`, the graded members whose
  grades the write overwrites; `confirm_write=True` over graded members
  is refused without that list, and refused when the members graded by
  then differ from it (for example a member graded after the preview).
  The result names the members whose grades were overwritten.
- The grade page must belong to the requested group. A redirect to another
  group's page (or to a page that does not name the group), a page for
  another `answer_group`, or a save URL outside the course and question or
  through another group's confirmed member refuses the write before
  anything is sent.
- Gradescope serves a group's grade page by redirecting to its
  representative submission in group mode, whose save URL already ends in
  `/save_many_grades`. That URL is used only when the page is in group mode
  for the requested group and saves through its own submission, which must
  be a confirmed member of the group. A save URL ending in `/save_grade` is
  still rewritten to `/save_many_grades`; any other URL is refused.
- A group with no confirmed members is refused before its grade page is
  fetched: Gradescope has no grading page for it (its grade URL redirects
  to the answer-grouping overview). The Error lists the inferred members;
  confirm the members in Gradescope's answer-grouping UI or grade the
  submissions individually. A grade page that lands on that overview for
  another reason is reported as such.
- Pass the member count from the preview as `expected_member_count`
  together with `confirm_write=True`; the write aborts if the group's
  membership changed since the preview.
- `rubric_item_ids=[]` clears every rubric item for every member and is
  allowed only together with a point adjustment or comment.

### Uploads
`tool_upload_submission` uploads as the logged-in account; each call creates
a new submission. Each path must be an absolute path to a regular file of at
most 100 MB. Hidden files and directories and credential-like names (keys,
`.env`, ...) are refused. When `GRADESCOPE_MCP_UPLOAD_ROOT` is set, files
must resolve inside one of its directories. When it is not set, symbolic
links and files under system directories (`/etc`, `/proc`, `/run`, ...) are
refused. The preview lists each file's size and SHA-256 and the
`expected_sha256` list to pass back with `confirm_write=True`. When it is
passed, a file whose content changed since the preview makes the call an
`Error` and nothing is uploaded. Without it, the result says the content
was not checked. Each file is read once, and the bytes read are the ones
hashed and sent.

Before uploading, the tool reads the assignment page to note the account's
existing submissions. Success is reported only when all of these hold:
- Gradescope answers the upload request itself with a redirect to a
  submission of this assignment
  (`/courses/<cid>/assignments/<aid>/submissions/<id>`).
- That submission was not among the existing ones.
- The final page is that submission's page, or a page below it such as
  the PDF page-selection step.
- The final page shows no visible error-styled message (`alert-danger`,
  `alert-error`, `flash-error` and the like). Hidden elements, JavaScript
  templates and `<noscript>` content are ignored.

Any other outcome is `❌ Upload not confirmed`, with the redirect target,
the final page and any message Gradescope showed (an error message is
quoted on its own line). A visible message that is not styled as an error
or a success (a warning, a bare `role="alert"`) does not turn a confirmed
upload into a failure: the success result quotes it under a ⚠️ line. A rejected upload that
ends on an older submission is therefore not reported as a success. Check
the assignment in Gradescope before uploading again, since every upload
creates a new submission.

### Scoring direction
- Gradescope questions use `positive` or `negative` scoring.
- When Gradescope does not report it, `tool_get_question_rubric`, the
  grading context and the regrade detail show it as unknown (for example
  `unknown (not reported by Gradescope; projections assume negative)`;
  JSON `scoring_type: null` plus `scoring_type_note`) instead of assuming a
  direction, and the grade previews warn that their projected scores
  assume deduction. No tool can resolve it: ask the user or check the
  question's scoring settings in Gradescope.
- Rubric weights are positive numbers in both modes; the scoring type
  decides whether a checked item adds or deducts points. Negative weights
  are rejected unless `allow_negative=True` is passed deliberately.

### Scanned / handwritten assignments
- Structured reference answers are often unavailable. This is expected, not
  necessarily a parsing failure.
- Students often tag the wrong pages. The workflow helpers use crop regions,
  the rest of the crop page, adjacent pages, then every other page, plus
  rubric text and user-provided reference notes.
- `tool_cache_relevant_pages` streams each page image and drops a page
  that exceeds 25 MB or takes more than 120 s (from the request on, also
  for a Gradescope-hosted URL answered as HTML); failed pages are listed
  and the rest are cached. A Gradescope-hosted page that turns out to be
  the logged-out page is a session expiry, as for any other request.
- Without a usable `assignment_id`, the workflow helpers find the question's
  assignment by scanning the course: unreadable assignments (no access,
  non-JSON pages) are skipped, while three non-JSON pages in a row or an
  authentication error stop the scan with an error.
- Readiness scores describe how much pre-read context exists (prompt,
  reference, rubric, crop regions, located student work). They are not
  grading confidence. Scanned exams usually show `partially_ready`.

### Local cache
- Root: `$GRADESCOPE_MCP_CACHE_DIR` if set, else
  `$XDG_RUNTIME_DIR/gradescope-mcp` (when that runtime directory exists and
  belongs to you), else `<system temp dir>/gradescope-mcp-<uid>`.
- Directories are created with mode 0700 and files with mode 0600, written
  atomically without following symlinks. The workflow tools print the real
  path of every file they write.
- The server refuses a root that is a symlink, owned by another user, or
  accessible to group/other: the workflow tools return an Error and the
  server logs the problem at startup. Fix it with `chmod 700 <dir>` or point
  `GRADESCOPE_MCP_CACHE_DIR` at another directory.
- At startup the server points its own `TMPDIR`/`TEMP`/`TMP` at the cache
  root and `XDG_CACHE_HOME` at a subdirectory of it, so temporary files stay
  private.
- The cache is ephemeral: `$XDG_RUNTIME_DIR` is normally cleared at logout,
  and the temp-directory fallback may be cleared at reboot. Do not rely on
  earlier artifacts in a new session.

## Configuration

| Variable | Required | Meaning |
|----------|----------|---------|
| `GRADESCOPE_EMAIL` | yes | Gradescope account email |
| `GRADESCOPE_PASSWORD` | yes | Gradescope account password |
| `GRADESCOPE_MCP_CACHE_DIR` | no | Private cache root (see [Local cache](#local-cache)); must be owned by you with mode 0700 |
| `GRADESCOPE_MCP_HTTP_TIMEOUT` | no | Read timeout in seconds for Gradescope requests (default 60); the connect timeout is `min(10, value)`. Invalid values are ignored with a warning |
| `GRADESCOPE_MCP_UPLOAD_ROOT` | no | Absolute paths of existing directories, separated by `os.pathsep` (`:` on Linux/macOS, `;` on Windows), that upload files must resolve inside |

At startup, `python -m gradescope_mcp` (and the `gradescope-mcp` console
script) loads `.env` files with python-dotenv from these places only:

1. `.env` in the current working directory;
2. `.env` in the gradescope-mcp source checkout the server runs from (this
   repository's `src/gradescope_mcp` layout, recognized by its
   `pyproject.toml`).

Variables already set in the environment (for example in the MCP client's
`env` block) always win, and the working directory's `.env` wins over the
checkout's. Parent directories are never searched, so a stray `.env` in
your home directory or next to an installed package is ignored. On Linux
and macOS a `.env` owned by another user (other than root) or writable by
everyone is skipped. The server logs which files it loaded or skipped.
With a non-editable install, start the server in a directory that holds
the `.env`, or pass the variables through the client configuration.

## Authentication

- Credentials come only from `GRADESCOPE_EMAIL` and `GRADESCOPE_PASSWORD`.
  The server logs in itself and POSTs them as a form body to `/login`
  (gradescopeapi would put them in the URL query string). Error messages and
  logs never contain them.
- A login that Gradescope answers without logging in starts a cooldown.
  Until it ends, calls with the same credentials fail immediately with the
  same `Authentication error: ...`, which ends with "Not trying to log in
  again for <wait>.", without contacting Gradescope:
  - invalid credentials (Gradescope shows the login form again or says the
    email or password is wrong): 10 minutes;
  - HTTP 429 or 5xx: Gradescope's `Retry-After`, or 1 minute without one;
  - a "too many login attempts" page: 5 minutes (or `Retry-After`);
  - any other rejection (`login rejected (HTTP <status>)`, no CSRF token
    after login, ...): 1 minute.

  A `Retry-After` wait is capped at 15 minutes. Network errors start no
  cooldown. Before every login attempt the server re-reads
  `GRADESCOPE_EMAIL` / `GRADESCOPE_PASSWORD` from the `.env` files it loaded
  at startup, so after fixing them in `.env` the next call logs in with the
  new credentials (no cooldown applies to changed credentials) — no restart
  needed. Credentials set in the MCP client's `env` block take precedence
  over `.env` and still need a server restart to change.
- Every request has a default timeout of 10 s to connect and 60 s to read
  (see `GRADESCOPE_MCP_HTTP_TIMEOUT`), so a stalled connection fails the
  call instead of hanging it.
- When Gradescope's session expires during a call (a redirect to the login
  page, the login page itself, a 401 "must be logged in", or the logged-out
  home page with the login form and no logout link, for example after a
  redirect to `/`), the tool or resource logs in again and re-runs once.
- A call is not re-run if Gradescope had already accepted a write during
  it: a re-run could repeat the write, or find it done and report that
  nothing changed. The result then ends with a "⚠️ The Gradescope session
  expired during this call after Gradescope had accepted N write
  request(s) ..." notice. Gradescope did receive those writes; anything the
  result reports as failed or not read back is unconfirmed, so check it
  with the read tools before retrying it.
- If the session expires again during the re-run, the result depends on
  whether Gradescope accepted a write during the re-run:
  - It did: the re-run's output is returned with the same "⚠️ ...
    accepted N write request(s) ..." notice. This is not an error result
    (`isError` is false unless the output itself starts with an error), so
    look for the notice rather than relying on `isError`. If the re-run
    raised instead of returning output, the result is
    `Authentication error: Gradescope session expired during the call after Gradescope had accepted N write request(s); ...`.
  - It did not: the result is
    `Authentication error: Gradescope session expired and re-login did not restore access.`
    (`isError: true`), followed by the output of the first attempt (or of
    the re-run, if the first attempt raised), labelled as possibly
    incomplete. If neither run returned output, the error stands alone.
- Only email/password login is supported.

## Architecture

### Entry points
- `src/gradescope_mcp/__main__.py`: `main()` loads the `.env` files (see
  [Configuration](#configuration)), sets up the private cache environment,
  configures logging, and runs the `MCPServer` (mcp v2) over stdio. The
  `gradescope-mcp` console script calls the same `main()`; importing the
  module has no side effects.
- `src/gradescope_mcp/server.py`: registers all tools, resources and
  prompts. Tools are registered with `gs_tool(annotations)` and resources
  with `gs_resource(uri)`; both add session recovery, and `gs_tool` adds the
  annotations, the title and the `isError` mapping. It also defines the
  argument types (`GradescopeID`, `Number`, `Count`) and the typed
  `GradeRow` batch rows.

### Runtime
- mcp v2 runs these sync tool functions on worker threads, so tool calls can
  run concurrently and pings are answered while a tool waits on Gradescope.
  A cancelled or timed-out call is not interrupted; its thread runs to the
  end (see [Known Caveats](#known-caveats)).
- `src/gradescope_mcp/auth.py`: one shared `GSConnection` behind a lock;
  form-body login, failed-login cooldowns, `TimeoutHTTPAdapter` default
  timeouts, a response hook that raises `SessionExpiredError` and counts the
  writes Gradescope accepted during the call, and `with_session_recovery`,
  which re-runs a call once after an expiry unless a write was accepted.
- `src/gradescope_mcp/cache.py`: the private per-user cache root and safe
  artifact writes.
- `src/gradescope_mcp/envfiles.py`: which `.env` files are loaded, in what
  precedence, and `refresh_credentials()`, which re-reads the credentials
  from those files before each login attempt.

### Tool modules
- `tools/courses.py`: course listing and roster parsing
- `tools/assignments.py`: assignment listing, date edits (and the shared
  date parser), rename, autograder image
- `tools/submissions.py`: uploads, submission listing, per-student
  submission reads, grader discovery
- `tools/extensions.py`: extension reads and writes
- `tools/grading.py`: outline parsing, score exports, grading progress,
  student submission links
- `tools/grading_ops.py`: grading context, grade writes (single and batch),
  rubric CRUD, question-submission discovery, navigation
- `tools/grading_workflow.py`: grading artifacts, answer keys, readiness,
  page caching, smart reading
- `tools/answer_groups.py`: answer-group inspection and batch writes
- `tools/regrades.py`: regrade listing and detail
- `tools/statistics.py`: assignment statistics
- `tools/lms_export.py`: Canvas / Brightspace gradebook import CSV export
- `tools/common.py`: shared helpers (rubric-ID normalization, untrusted-text
  blocks, markdown escaping, one-line display names, page selection)
- `tools/safety.py`: write-preview messages and shared per-object write locks
- `tools/outline_data.py`: shared PDF/online outline parser
- `tools/outline.py`: revision-checked outline writes
- `inspect.py`: offline registry inventory
- `tests/test_outline.py`, `tests/test_inspect.py`: outline and inventory checks

## Quick Start

### 1. Prerequisites
- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

### 2. Install
```bash
git clone https://github.com/Yuanpeng-Li/gradescope-mcp.git
cd gradescope-mcp
cp .env.example .env
```

Then edit `.env` with your Gradescope credentials (and any optional
settings).

### 3. Run locally
```bash
uv run python -m gradescope_mcp
```

### 4. Configure an MCP client

Example client configuration:

```json
{
  "mcpServers": {
    "gradescope": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/gradescope-mcp",
        "python",
        "-m",
        "gradescope_mcp"
      ],
      "env": {
        "GRADESCOPE_EMAIL": "your_email@example.com",
        "GRADESCOPE_PASSWORD": "your_password"
      }
    }
  }
}
```

Credentials in a client configuration file are stored in plain text; keep
that file private (this repository ignores `.mcp.json` and `.env`).

### 5. Debug with MCP Inspector
```bash
npx @modelcontextprotocol/inspector uv run python -m gradescope_mcp
```

### 6. Run tests
```bash
uv run pytest -q
```

The tests are offline: they use fakes, a per-test cache directory and no
credentials.

## Assisted Grading Skill

The repository includes one skill:

- `gradescope-assisted-grading`

It is intended for:
- preview-first grading
- rubric review before mutation
- scanned exam grading
- answer-group triage
- explicit human approval before any grade write

### Install the skill

Link (or copy) the skill directory into your client's skills directory. For
Claude Code, personal skills live in `~/.claude/skills/<name>/` and project
skills in `.claude/skills/<name>/` of the project you work in.

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/gradescope-assisted-grading" ~/.claude/skills/gradescope-assisted-grading
```

If you prefer copying (re-copy after updating this repository):

```bash
mkdir -p ~/.claude/skills
cp -R skills/gradescope-assisted-grading ~/.claude/skills/
```

For other clients, put the directory wherever that client loads skills from.

### Verify installation
```bash
ls ~/.claude/skills/gradescope-assisted-grading
head -n 5 ~/.claude/skills/gradescope-assisted-grading/SKILL.md
```

In Claude Code, invoke it with `/gradescope-assisted-grading` or by asking
"Use the gradescope-assisted-grading skill".

## Project Structure

```text
gradescope-mcp/
├── .env.example
├── .gitignore
├── .python-version
├── AGENT.md
├── DEVLOG.md
├── LICENSE
├── README.md
├── pyproject.toml
├── uv.lock
├── skills/
│   └── gradescope-assisted-grading/
│       └── SKILL.md
├── src/
│   └── gradescope_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── auth.py
│       ├── cache.py
│       ├── envfiles.py
│       ├── server.py
│       └── tools/
│           ├── __init__.py
│           ├── answer_groups.py
│           ├── assignments.py
│           ├── common.py
│           ├── courses.py
│           ├── extensions.py
│           ├── grading.py
│           ├── grading_ops.py
│           ├── grading_workflow.py
│           ├── lms_export.py
│           ├── regrades.py
│           ├── safety.py
│           ├── statistics.py
│           └── submissions.py
└── tests/
    ├── conftest.py
    ├── test_answer_groups.py
    ├── test_assignments_and_grading_ops.py
    ├── test_auth.py
    ├── test_common.py
    ├── test_dates_extensions_submissions.py
    ├── test_docs_consistency.py
    ├── test_extensions_and_answer_key.py
    ├── test_grading_ops_fixes.py
    ├── test_grading_workflow.py
    ├── test_live_fixes.py
    ├── test_lms_export.py
    ├── test_p0_fixes.py
    ├── test_page_selection.py
    ├── test_read_side_fixes.py
    ├── test_round3_runtime.py
    ├── test_server_mcp.py
    ├── test_session_recovery.py
    ├── test_workflow_fixes.py
    └── test_write_safety.py
```

## Development Notes

- `AGENT.md` summarizes the architecture and the rules for changing tools.
- `DEVLOG.md` records the implementation history.
- `tests/test_docs_consistency.py` checks this file, `AGENT.md` and the skill
  against the registered tools, resources, prompts and environment
  variables.
- If you test writes against a real account, keep the log of those changes
  outside the repository. `OPERATIONS_LOGS/` is gitignored for that purpose
  and is not part of the repository.

## Known Caveats

1. Gradescope behavior differs across assignment types; several tools rely on
   HTML parsing or reverse-engineered endpoints, and some write behavior
   (for example how `save_many_grades` treats inferred members) can't be
   verified offline. The tools choose the conservative option and say so.
   The pages the date and group-grading writes read (the settings page's
   due-date data, the group grade page's redirect to a group-mode
   submission page) were checked read-only against a live account; the
   writes themselves have not been run against one.
2. Roster parsing uses a custom parser because the upstream library parser is
   unreliable when sections are present.
3. Some assignment types do not support the extensions API even for staff
   users, and removing an extension is not supported. `tool_set_extension`
   refuses when the extensions page has neither the extensions table nor
   an existing extension; if Gradescope renders such a page for an
   assignment without extensions, add the first one in the web UI.
4. Scanned assignments usually do not provide a structured answer key.
5. Question grading requires Question Submission IDs, not assignment-level
   Global Submission IDs.
6. mcp runs each tool call on a worker thread and does not stop it when the
   client cancels or times out. The call runs to the end (a batch writes
   its remaining rows, at most 50) and the client never sees its result.
   After a timed-out write, re-read the affected submissions or settings
   before retrying.
7. Date and extension writes are serialized only within one server process.
   Each confirmed write re-reads the current settings, but a change made
   elsewhere (another server process, the web UI) between that read and
   the write can still be overwritten.

Outline weights returned as decimal strings (for example `"4.0"`) are normalized for preview/save and compared numerically on read-back, including subquestions. Existing saved crop proposals can be reused without changing their coordinates.

- `tools/question_settings.py`: scoring options, grading-method selection and manual answer-group creation.
- `tests/test_question_settings.py`: preview, zero-weight preservation, revisions and partial failures.

Zero-point bonus workflow: preview `tool_set_question_scoring` with scoring_type="positive", ceiling=False (floor omitted to preserve it); confirm only after explicit user approval with expected_revision. Weight is never sent and must remain unchanged on read-back. Use `tool_set_question_grading_method` with method="individual" for per-student batch grading or method="groups" for manual grouping. apply_to_all previews every leaf question in the assignment and writes sequentially, so partial completion is possible. AI methods require previously configured answer regions. `tool_create_answer_group` accepts only explicitly ungraded, ungrouped Question Submission IDs; creation and membership are separate steps, and partial failures report the created group ID. Inspect before retrying. These tools do not submit rubric items or grades, but question settings can change existing computed scores. Live mutation behavior has not been tested.

- `operations.py`: lifecycle implementation and regression coverage.

- `tools/grading_plans.py`: lifecycle implementation and regression coverage.

- `tests/test_grading_plans.py`: lifecycle implementation and regression coverage.

- `tools/assignment_admin.py`: lifecycle implementation and regression coverage.

- `tools/rubric_plans.py`: lifecycle implementation and regression coverage.

- `tools/pdf_regions.py`: lifecycle implementation and regression coverage.

- `tests/test_assignment_admin.py`: lifecycle implementation and regression coverage.

- `tests/test_rubric_plans.py`: lifecycle implementation and regression coverage.

- `tests/test_pdf_regions.py`: lifecycle implementation and regression coverage.

- `tools/submission_files.py`: lifecycle implementation and regression coverage.

- `tools/post_grading.py`: lifecycle implementation and regression coverage.

- `tools/review.py`: lifecycle implementation and regression coverage.

- `tests/test_lifecycle_review.py`: lifecycle implementation and regression coverage.

- `tools/blueprints.py`: lifecycle implementation and regression coverage.

- `tools/autograder.py`: lifecycle implementation and regression coverage.

- `tests/test_autograder.py`: lifecycle implementation and regression coverage.

- `tests/test_blueprints.py`: lifecycle implementation and regression coverage.

- `tools/rubric_data.py`: pre-submission AssignmentRubric normalization.

Final review hardening: cancellation propagates through assignment-copy child operations and is checked immediately before writes; all-question grading-method changes acquire question locks in a fixed order. Assignment settings reads use the same bare field names accepted by options, and PATCH sends only requested attributes. PDF template saves are verified against downloaded bytes when available; inability to verify remains explicit. Operation journals support paginated summaries and detailed row reads. Trusted-storage HTML submissions may be downloaded as inert files, while unexpected same-site HTML is rejected.
