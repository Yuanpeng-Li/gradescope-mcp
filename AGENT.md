# Gradescope MCP Server Developer Guide

## Project Objective

This project exposes Gradescope course-management and grading capabilities
through the MCP Python SDK v2 (`MCPServer`) so that MCP clients can inspect
courses, plan grading, review student work, and execute carefully gated write
operations.

The codebase is optimized for real-world instructor and TA workflows, especially
for scanned exams and AI-assisted grading where the client needs more than just
simple CRUD wrappers.

## Current Snapshot

- 76 tools (34 read-only, 27 Gradescope writes, 15 local cache writes)
- 3 resources (1 static, 2 URI templates)
- 7 prompts
- Offline pytest suite (the current count is recorded in `DEVLOG.md`)
- Python 3.10+
- `uv` + `hatchling`
- `mcp` v2 `MCPServer` (sync tools run on worker threads, not the event loop)
- `gradescopeapi` plus direct HTTP/HTML/JSON scraping for unsupported cases

## Repository Map

### Runtime entry
- `src/gradescope_mcp/__main__.py`
  `main()` loads the `.env` files (`envfiles.load_env_files`, re-exported
  here), calls
  `cache.configure_process_cache_env()` (an unsafe cache root is logged, not
  fatal), configures logging, logs the `.env` files loaded or skipped, and
  starts the server. Nothing runs at import time.
- `src/gradescope_mcp/envfiles.py`
  `.env` discovery and loading. `dotenv_candidates` and
  `source_checkout` define the search: `.env` in the working directory,
  then in the gradescope-mcp source checkout (`<checkout>/src/gradescope_mcp`
  with a `pyproject.toml` naming the project); parents are never searched,
  values already in the environment win (`override=False`), and on POSIX a
  file owned by another non-root user or writable by everyone is skipped.
  `main()` passes `remember_credentials=True`; `refresh_credentials()`
  (called by `auth.get_connection()` before every login attempt) then
  re-reads `GRADESCOPE_EMAIL` / `GRADESCOPE_PASSWORD` from the same files,
  touching only the credential variables that came from `.env`.

### Server registration
- `src/gradescope_mcp/server.py`
  The authoritative tool/resource/prompt inventory. If counts in docs
  disagree, trust this file (and `tests/test_docs_consistency.py` will fail).
  - `gs_tool(annotations)` registers a tool: it wraps the function in
    `with_session_recovery`, then `_signal_errors` (handled-failure text
    becomes `isError: true`; an escaped `AuthError` becomes an
    `Authentication error: ...` result; a successful result whose whole
    text is one JSON object also gets it as `structuredContent`, see
    `structured_json`), and registers it with the annotations, a title and
    `structured_output=False` (no `outputSchema`).
  - Annotation helpers: `read_only(title)`, `gradescope_write(title,
    idempotent=...)` and `local_cache_write(title)`.
  - `gs_resource(uri)` registers a resource with session recovery; failure
    text is raised as `ResourceError`. Template IDs are validated with
    `_resource_id` (`ResourceNotFoundError` otherwise).
  - Argument types: `GradescopeID` (ASCII digits only, schema pattern
    `^[0-9]+$`; JSON numbers accepted, whitespace and backticks stripped,
    leading zeros dropped so `"031"` becomes `"31"`), `OptionalGradescopeID`
    (blank or null means not given), `Number` (a float that rejects JSON
    booleans), `Count` (an int >= 0 that rejects booleans), `OutputFormat`,
    the strict `GradeRow` TypedDict for `tool_apply_grade_batch`, and
    `GradeRows` (a list of `GradeRow` advertising `maxItems` =
    `grading_ops.MAX_BATCH_ROWS`; `apply_grade_batch` itself refuses longer
    batches with its own Error, so the schema does not enforce it).
  - The prompts. They make no Gradescope requests and are registered with a
    plain `@mcp.prompt()`.

### Auth and session reuse
- `src/gradescope_mcp/auth.py`
  - `get_connection()` returns one process-wide `GSConnection`, created and
    dropped under a lock. The module logs in itself: credentials are POSTed
    as a form body, never put in a URL, and scrubbed from every error and
    log line.
  - A login Gradescope answers without logging in raises `_LoginRejected`
    (`_InvalidCredentials` for a re-rendered login form or an "invalid
    email/password" text) and is stored as a `_LoginFailure` (credential
    fingerprint, scrubbed reason, retry time on the `_clock` seam). Until
    the cooldown ends, calls with the same credentials get the same
    `AuthError`, ending in "Not trying to log in again for <wait>.":
    `INVALID_CREDENTIALS_COOLDOWN` (10 min), `THROTTLED_LOGIN_COOLDOWN`
    (429/5xx without `Retry-After`, 1 min), `TOO_MANY_ATTEMPTS_COOLDOWN`
    (5 min), `REJECTED_LOGIN_COOLDOWN` (any other rejection, 1 min);
    `Retry-After` is honoured up to `MAX_LOGIN_COOLDOWN` (15 min). Network
    errors are not cached.
  - `TimeoutHTTPAdapter` is mounted on the session for http and https and
    applies `DEFAULT_TIMEOUT` (10 s connect, 60 s read) to every request
    without its own timeout, including gradescopeapi's requests.
    `GRADESCOPE_MCP_HTTP_TIMEOUT` sets the read timeout and caps the connect
    timeout.
  - After login, a response hook raises `SessionExpiredError` (an
    `AuthError`) on a redirect to `/login` or `/account/auth` (before the
    redirect is followed), on the login page itself, on a 401 "must be
    logged in", or on the logged-out home page (a same-site HTML page with
    a form posting to `/login` and no `/logout` link), and flags the
    current thread (`_local.expired`). It never reads a streamed body: for
    a streamed same-site HTML answer (page downloads) it leaves the
    logged-out-page check to the reader, which calls
    `check_streamed_body(resp, body)` after its bounded read, so the
    download's size cap and deadline hold. Responses that are not expiry
    signals go through `_note_write`, which counts the same-site write
    requests (not GET/HEAD/OPTIONS/TRACE) Gradescope answered with a 2xx in
    `_local.writes`; a write answered with a redirect stays
    `_local.pending_write` until the next response shows whether it hit an
    expiry.
  - `with_session_recovery(fn)` (applied by `gs_tool` / `gs_resource`) sees
    the flag and calls `reset_connection(expired=<that connection>)`. If no
    write was accepted during the call it re-runs the call once on a fresh
    login. If one was, it does not re-run it (that could repeat the write,
    or find it done and report "nothing changed"): the first result is
    returned with a "session expired ... after Gradescope had accepted N
    write request(s)" notice. If the re-run expires too, a re-run that
    had a write accepted is reported the same way
    (`_report_writes_then_expiry`): its output plus the notice, which is
    not an error result (`isError` false), or `SessionExpiredError` if it
    raised. A re-run that wrote nothing returns
    `SESSION_RECOVERY_FAILED_MESSAGE` followed by the output of the first
    run that returned text (normally the first attempt, labelled as
    possibly incomplete) instead of replacing it
    (`_report_failed_recovery`); if neither returned text it raises
    `SessionExpiredError`.
    `reset_connection()` without `expired` also does a best-effort logout.
  - The module-level `tool_*` / `resource_*` names in `server.py` are the
    wrapped functions (the original is at `__wrapped__`).

### Cache
- `src/gradescope_mcp/cache.py`
  The private per-user cache root: `GRADESCOPE_MCP_CACHE_DIR`, else
  `$XDG_RUNTIME_DIR/gradescope-mcp`, else `<tempdir>/gradescope-mcp-<uid>`.
  The root must be a real directory owned by the user with mode 0700, or
  `CacheError` is raised. Use `get_artifact_path`, `get_artifact_dir` and
  `write_artifact` (0600, `O_EXCL | O_NOFOLLOW` temp file plus atomic
  rename); never build cache paths by hand.

### Tool modules
- `src/gradescope_mcp/tools/courses.py`
  Course listing and custom roster parsing.
- `src/gradescope_mcp/tools/assignments.py`
  Assignment listing, detail reads, date edits (merged and verified), rename,
  autograder image. The listing fetches the page itself with gradescopeapi's
  parsers so it can also name the assignment containers they drop. Date
  edits read the current values from the settings page's
  `SetupDueDateFormGroup` React props (the date inputs are rendered
  client-side; inputs are the fallback) and post gradescopeapi's form field
  names. Also `parse_date_input` / `check_date_order` and
  `serialized_write` (a process-wide lock per Gradescope object, held across
  read, write and read-back of a confirmed write; waits up to 300 s, then
  `WriteInProgressError`), which `extensions.py` reuses.
- `src/gradescope_mcp/tools/submissions.py`
  Uploads (path vetting, `GRADESCOPE_MCP_UPLOAD_ROOT`), submission listing,
  per-student submission reads, grader discovery.
- `src/gradescope_mcp/tools/extensions.py`
  Extension reads (course-local time = UTC instant, other settings) and
  writes (course timezone resolution). `set_extension` re-sends the
  student's whole current override with the requested dates replaced plus
  `visible=true`, so callers need not re-pass existing dates; it refuses
  when the extensions page can't be read and reports settings the
  read-back shows dropped or changed.
- `src/gradescope_mcp/tools/grading.py`
  Outline parsing, score export, grading progress, student submission links.
- `src/gradescope_mcp/tools/grading_ops.py`
  Submission grading context, grade writes (single and batch), confidence
  thresholds (`CONFIDENCE_REJECT_BELOW`, `CONFIDENCE_REVIEW_UP_TO`), the
  batch cap `MAX_BATCH_ROWS` (50), rubric CRUD, question-submission
  discovery, navigation. Grade writes re-read each submission at write time
  and never overwrite a graded one without that submission's own approval
  (`overwrite_graded=True` for `apply_grade`, `"overwrite": true` on the
  batch row; there is no batch-wide flag), don't re-send a grade the
  submission already holds, and refuse a grading page whose save URL
  targets another submission (`_write_target_problem`). The batch preview
  refuses `"overwrite": true` on a row that is not graded or already holds
  the requested grade.
- `src/gradescope_mcp/tools/grading_workflow.py`
  Workflow helpers that write artifacts to the private cache, compute
  readiness (pre-read context, not grading confidence), cache pages, and
  build crop-first read plans.
- `src/gradescope_mcp/tools/answer_groups.py`
  AI-assisted answer-group inspection and batch grading. The grade page must
  belong to the requested group (`_group_page_problem`). Gradescope serves it
  as the representative submission's group-mode page, whose save URL
  already ends in `/save_many_grades` (`_group_mode_problem` ties it to the
  group); a group without confirmed members is refused before its page is
  fetched.
- `src/gradescope_mcp/tools/regrades.py`
  Regrade list/detail scraping.
- `src/gradescope_mcp/tools/statistics.py`
  Assignment statistics.
- `src/gradescope_mcp/tools/markup_check.py`
  `math_markup_warnings`: advisory Markdown/KaTeX delimiter checks (doubled
  `$$`, unclosed `$`, unbalanced braces) for comments and rubric text; never
  blocks a write.
- `src/gradescope_mcp/tools/lms_export.py`
  `export_lms_gradebook`: builds a Canvas or Brightspace gradebook import CSV
  from the assignment's `/scores` export and writes it to the private cache
  (only fully graded scores by default; missing → blank or 0; partial totals
  opt-in).
- `src/gradescope_mcp/tools/common.py`
  Shared helpers: `normalize_rubric_ids`, `split_known_rubric_ids`,
  `format_untrusted` (fenced block whose BEGIN and END lines carry a random
  per-call block id; marker and backtick runs in the text are broken with
  zero-width spaces), `escape_md_cell`, `sanitize_inline` (one-line
  student names), `normalize_url`, `is_placeholder_page`,
  `select_crop_pages`, `page_number`.
- `src/gradescope_mcp/inspect.py`
  Offline public-registry inspection; never authenticates.
- `src/gradescope_mcp/tools/outline_data.py`
  Shared editor parsing for both read and write paths.
- `src/gradescope_mcp/tools/outline.py`
  Outline preview/write with revisions, negative temporary online IDs, and read-back.
- `src/gradescope_mcp/tools/safety.py`
  Shared per-object write locks and confirmation-preview helper for mutations; its last line tells the
  agent to show the preview and re-run with `confirm_write=True` only after
  the user explicitly approves.

## Tool Inventory

Grouped by annotation class. `tests/test_server_mcp.py` pins these sets.

### Read-only
`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`.

1. `tool_list_courses`
2. `tool_get_assignments`
3. `tool_get_assignment_details`
4. `tool_get_course_roster`
5. `tool_get_extensions`
6. `tool_get_assignment_submissions`
7. `tool_get_student_submission`
8. `tool_get_assignment_graders`
9. `tool_get_assignment_outline`
10. `tool_export_assignment_scores`
11. `tool_get_student_assignment_link`
12. `tool_get_grading_progress`
13. `tool_get_regrade_requests`
14. `tool_get_regrade_detail`
15. `tool_get_assignment_statistics`
16. `tool_get_submission_grading_context`
17. `tool_get_question_rubric`
18. `tool_list_question_submissions`
19. `tool_get_student_submission_map`
20. `tool_get_next_ungraded`
21. `tool_get_answer_groups`
22. `tool_get_answer_group_detail`
23. `tool_assess_submission_readiness`
24. `tool_smart_read_submission`

25. `tool_get_operation`

26. `tool_calibrate_grade_plan`

27. `tool_get_assignment_admin`

28. `tool_export_question_rubric`

29. `tool_inspect_submission_pdf`

30. `tool_review_assignment`

31. `tool_sample_question_submissions`

32. `tool_check_archive_freshness`

33. `tool_get_autograder_status`

34. `tool_get_autograder_results`

### Gradescope writes
`destructiveHint=true`, exactly the tools with `confirm_write`.
`idempotentHint=false` for upload, outline additions, answer-group creation/merging and rubric-item creation, true otherwise.
Idempotent follows the MCP definition (a repeat with the same arguments has
no additional effect), not "returns the same result": a repeated delete
reports the item missing, and a repeated group grade is refused because
the first call graded the members (without `overwrite_graded`, or with
an `expected_graded_ids` that no longer matches the graded members);
nothing is sent.

35. `tool_upload_submission`
36. `tool_set_extension`
37. `tool_modify_assignment_dates`
38. `tool_rename_assignment`
39. `tool_update_autograder_image`
40. `tool_apply_grade`
41. `tool_apply_grade_batch`
42. `tool_create_rubric_item`
43. `tool_update_rubric_item`
44. `tool_delete_rubric_item`
45. `tool_grade_answer_group`

46. `tool_update_assignment_outline`

47. `tool_set_question_scoring`
48. `tool_set_question_grading_method`
49. `tool_create_answer_group`

50. `tool_execute_grade_plan`

51. `tool_execute_assignment_plan`

52. `tool_execute_rubric_plan`

53. `tool_resume_rubric_plan`

54. `tool_patch_assignment_outline`

55. `tool_respond_regrade_request`

56. `tool_modify_extension_options`

57. `tool_set_answer_group_members`

58. `tool_execute_assignment_clone`

59. `tool_execute_autograder_plan`

60. `tool_merge_answer_groups`

61. `tool_resume_operation`

### Local cache writes
`readOnlyHint=false`, `destructiveHint=false`: they read Gradescope and
write only to the private cache.

62. `tool_prepare_grading_artifact`
63. `tool_cache_relevant_pages`
64. `tool_prepare_answer_key`
65. `tool_export_lms_gradebook`

66. `tool_prepare_grade_plan`

67. `tool_cancel_operation`

68. `tool_prepare_assignment_plan`

69. `tool_prepare_rubric_plan`

70. `tool_preview_outline_regions`

71. `tool_download_submission_files`

72. `tool_archive_assignment`

73. `tool_prepare_choice_bonus_plan`

74. `tool_prepare_assignment_clone`

75. `tool_prepare_autograder_plan`

76. `tool_poll_operation`

All tools are `openWorldHint=true`.

## Rules For Changing Tools

- Register new tools with `@gs_tool(<annotation helper>)` and new resources
  with `@gs_resource(uri)` in `server.py`, never with a bare `@mcp.tool()` /
  `@mcp.resource()`: those would skip session recovery, `isError` mapping
  and annotations.
- Type every Gradescope ID parameter as `GradescopeID` (or
  `OptionalGradescopeID`), number parameters as `Number` / `Count` (plain
  `float` / `int` would accept `true` as 1), and use `Literal` enums for
  fixed choices.
- A tool that changes Gradescope data takes `confirm_write: bool = False`,
  uses `gradescope_write(...)`, validates everything before the confirm
  gate, previews exactly what it will send (`tools/safety.py`), and reads
  the result back where practical. Its docstring says that `confirm_write`
  is not user approval.
- Handled failures return text starting with `Error:` (`Authentication
  error:` for `AuthError`, `❌` for a write Gradescope rejected). Previews,
  confidence rejections, `⚠️` warnings and "No ... found" messages must not
  use those prefixes.
- Wrap student-authored text with `format_untrusted`, escape user or
  student data in markdown table cells with `escape_md_cell`, and put
  student names on other markdown lines through `sanitize_inline`.
- Tool modules must not add their own session-expiry handling or
  `timeout=` arguments; `auth.py` handles both. Keep catching `AuthError`
  (`SessionExpiredError` is a subclass) and returning
  `Authentication error: ...`.
- Write cache files only through `cache.py`.
- Keep the wrapper's parameters in sync with the implementation
  (`tests/test_server_mcp.py` checks parity and pass-through).

## Operating Assumptions

### Authentication
- Credentials must come from `GRADESCOPE_EMAIL` and `GRADESCOPE_PASSWORD`
- Never hardcode credentials
- `python -m gradescope_mcp` loads `.env` from the working directory, then
  from the source checkout (never from parent directories); variables
  already in the environment win
- A rejected login starts a cooldown (at most 15 min) stated in the error.
  Credentials are re-read from the startup `.env` files before every login
  attempt (`envfiles.refresh_credentials`), so a fix in `.env` applies on the
  next call; credentials from the client's `env` block need a restart

### Write safety
- Every mutating tool is preview-first
- `confirm_write=False` must remain a no-op preview path
- `confirm_write=True` is required for actual mutation, and is not human
  approval: prompts and the skill require a shown preview and explicit
  user approval first
- Rubric updates and deletions are cascading operations
- `tool_execute_rubric_plan` runs a plan once; a stopped plan continues only
  through `tool_resume_rubric_plan`, which re-reads the rubric and reconciles
  each uncertain row first (an already-created item is adopted by ID, never
  created twice; ambiguity stops as `needs_review`). Rubric operation records
  carry `progress` (total/verified/failed/pending/current_index)
- `tool_list_courses` parses the account page itself (`courses._parse_account_courses`),
  not gradescopeapi's `get_courses`; an unreadable page is an error, never
  an empty list
- Batch answer-group writes can affect many submissions at once, including
  inferred members; already-graded members need `overwrite_graded=True`
  plus the `expected_graded_ids` the preview printed (refused, nothing
  sent, when the members graded at write time differ)
- `tool_apply_grade` and `tool_apply_grade_batch` re-read each submission
  when writing and never overwrite a graded one (including one graded after
  the preview) without its own approval: `overwrite_graded=True` for
  `tool_apply_grade`, `"overwrite": true` on that batch row (there is no
  batch-wide flag; a client still sending `overwrite_graded` to the batch
  gets graded rows skipped); a batch takes at most 50 rows
- Date and extension writes are serialized per assignment / per student
  within the process

### Error signalling
- `Error...`, `Authentication error...` and `❌...` results are returned with
  `isError: true` and unchanged text
- Schema validation failures come back as
  `Error executing tool <name>: ... validation error ...` with `isError: true`
- Tools publish no `outputSchema`. Every result has text content; a
  successful result whose whole text is one JSON object (at most
  `MAX_STRUCTURED_TEXT`, 1 MiB; no `NaN`) also carries that object as
  `structuredContent`. Markdown, error results, and JSON followed by a
  session-expiry notice stay text only (so the notice cannot be lost by a
  client that reads only `structuredContent`)

### ID semantics
- All IDs are ASCII digit strings without leading zeros (numbers accepted
  and IDs canonicalized at the MCP layer)
- Assignment-level submission listings return Global Submission IDs
- Grading operations need Question Submission IDs
- If a grading call returns 404, suspect the wrong ID type first

### Scoring semantics
- Gradescope questions can be `positive` or `negative`
- Rubric weights remain positive in both modes; negative weights need
  `allow_negative=True`
- The scoring mode determines whether checked items add or deduct points
- A missing `scoring_type` is reported as unknown (JSON `null` plus
  `scoring_type_note`), never defaulted; projections then assume deduction
  and previews say so
- `CONFIDENCE_REJECT_BELOW` (0.6) and `CONFIDENCE_REVIEW_UP_TO` (0.8):
  below 0.6 nothing is written; 0.6 to 0.8 inclusive is written and flagged
  NEEDS HUMAN REVIEW

### Dates
- Inputs are `YYYY-MM-DDTHH:MM` with an explicit time
- Assignment dates are course-local wall-clock times without an offset;
  omitted dates and the late-submission flag are preserved; previews and
  results name the course timezone from the settings page and warn when
  the due date is synced from an LMS
- Extension dates without an offset use the course timezone Gradescope
  reports; the `timezone` argument only stands in when it reports none (a
  differing zone, or one that can't be checked because several or an
  unknown zone are reported, is an Error), and a stand-in contradicted by
  the read-back after the write is a ⚠️ warning; dates with an offset are
  absolute
- `tool_set_extension` keeps the student's other current extension
  settings (it re-sends them), so existing dates need not be passed again

### Scanned assignment behavior
- Missing structured answer keys are common and expected
- Workflow helpers deliberately fall back to rubric + prompt + page evidence
- Page images and artifacts are written to the private runtime cache; the
  tools print the real paths
- Readiness is pre-read context, never a grading gate

## Testing And Data Hygiene

- Tests are offline. `tests/conftest.py` gives every test its own
  `GRADESCOPE_MCP_CACHE_DIR` and removes the credentials, so no test writes
  into a shared cache or logs in.
- Tests use fakes for Gradescope; MCP-layer behavior is tested through
  `server.mcp.call_tool(...)` or `mcp.Client(server.mcp)`.
- The project is designed for real Gradescope data and can touch sensitive
  student information during manual validation. Keep any log of real-account
  mutation tests outside the repository (`OPERATIONS_LOGS/` is gitignored
  for that and is not tracked).
- Do not include student names, IDs, grades, or raw submissions in
  repository logs.

Current test files:
- `tests/test_answer_groups.py`
- `tests/test_assignments_and_grading_ops.py`
- `tests/test_outline.py`
- `tests/test_inspect.py`
- `tests/test_auth.py`
- `tests/test_common.py`
- `tests/test_course_list_parser.py`: offline account-page fixtures for `list_courses`.
- `tests/test_dates_extensions_submissions.py`
- `tests/test_docs_consistency.py`
- `tests/test_extensions_and_answer_key.py`
- `tests/test_grade_executor.py`
- `tests/test_grading_ops_fixes.py`
- `tests/test_grading_workflow.py`
- `tests/test_live_fixes.py`
- `tests/test_lms_export.py`
- `tests/test_p0_fixes.py`
- `tests/test_page_selection.py`
- `tests/test_read_side_fixes.py`
- `tests/test_round3_runtime.py`
- `tests/test_server_mcp.py`
- `tests/test_session_recovery.py`
- `tests/test_structured_output.py`
- `tests/test_workflow_fixes.py`
- `tests/test_write_safety.py`

## Commands

```bash
uv run python -m gradescope_mcp
uv run pytest -q
npx @modelcontextprotocol/inspector uv run python -m gradescope_mcp
```

## Known Caveats

1. Several endpoints are reverse-engineered and can break if Gradescope changes
   its frontend payloads. Where server behavior can't be observed offline,
   the code takes the conservative option and says so.
2. `courses.py` uses a custom roster parser because upstream parsing is not
   reliable with sections.
3. Some assignment types return 401 for extension APIs even for staff users.
4. `get_next_ungraded` walks the question's own submissions listing and
   never follows links into another question.
5. Cache artifacts are ephemeral files, not durable project state.
6. mcp does not interrupt a tool call that the client cancels or that times
   out; the worker thread runs to the end (a batch keeps writing its
   remaining rows, at most `MAX_BATCH_ROWS`).
7. The per-object write locks serialize date, extension and outline writes only
   within one server process.

## Maintenance Rule

When code changes alter capabilities, update `README.md`, this file,
`skills/gradescope-assisted-grading/SKILL.md` and `DEVLOG.md` in the same
change. `tests/test_docs_consistency.py` catches inventory and count drift,
but not wording; check the prose against the code.

For the architecture, upstream audit, capability gaps and outline payload examples, see README.md, “Architecture and capability audit”. Do not advertise unimplemented upstream methods as working tools. The `assignments.serialized_write` wrapper remains for compatibility; the lock registry lives in `tools/safety.py`.

Outline weights returned as decimal strings (for example `"4.0"`) are normalized for preview/save and compared numerically on read-back, including subquestions. Existing saved crop proposals can be reused without changing their coordinates.

- `tools/question_settings.py`: scoring options preserve weight; grouping method and manual group writes use previews and revisions.
- `tests/test_question_settings.py`: question settings and manual grouping regressions.

Zero-point bonus workflow: preview `tool_set_question_scoring` with scoring_type="positive", ceiling=False (floor omitted to preserve it); confirm only after explicit user approval with expected_revision. Weight is never sent and must remain unchanged on read-back. Use `tool_set_question_grading_method` with method="individual" for per-student batch grading or method="groups" for manual grouping. apply_to_all previews every leaf question in the assignment and writes sequentially, so partial completion is possible. AI methods require previously configured answer regions. `tool_create_answer_group` accepts only explicitly ungraded, ungrouped Question Submission IDs; creation and membership are separate steps, and partial failures report the created group ID. Inspect before retrying. These tools do not submit rubric items or grades, but question settings can change existing computed scores. Live mutation behavior has not been tested.

- `operations.py`: lifecycle implementation and regression coverage.

- `tools/grading_plans.py`: lifecycle implementation and regression coverage.

- `tests/test_grading_plans.py`: lifecycle implementation and regression coverage.

- `tests/test_grade_executor.py`: grade-plan executor: pooled reads/writes and auth state merging, progress, background runs, resume reconciliation, score verification, compact output.

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

Lifecycle plans are private operation records in `operations.py`. Prepare/show/approve/execute using request_digest and operation_token; an uncertain or partially accepted write is not permission to replay it.

Grade plans (`tools/grading_plans.py`):
- `prepare_grade_plan` reads the grading pages on a pool
  (`GRADESCOPE_MCP_READ_CONCURRENCY`, 1–8, default 4) through
  `_concurrent_map`, which keeps input order, raises the lowest-index
  failure (what a sequential loop would raise) and merges each pool
  thread's auth call state back (`auth.run_with_call_state` /
  `auth.merge_call_state`). Any new pooled Gradescope work must do the
  same, or `with_session_recovery` misses expiries and accepted writes and
  could re-run a call after a write.
- Execution re-reads every row right before its write (never a
  prefetch), journals `sending` before the POST and verifies the read-back:
  `verified` needs no mismatches, `graded` true and the read-back score
  equal to `projected_score` (1e-6); otherwise `score_mismatch` or
  `accepted_unverified` with a `verification_note`. The same rule applies
  when Gradescope already holds the planned grade (`reconciled`). Rows run
  one at a time by default; `GRADESCOPE_MCP_WRITE_CONCURRENCY` (1–4) runs
  whole rows side by side, journal updates serialized by `_Run.lock`, no
  new row after the first problem, in-flight rows finish.
- `progress` (per-run counters plus cumulative state counts, current row,
  timestamps) is saved with every row transition; `get_operation` shows it
  with `executor_active`.
- `background=True` claims the operation's run lock in the tool call and
  hands it to a daemon thread that takes the usual operation/question
  locks. A second execute/resume gets "already running" at once. The
  thread has no session recovery: an `AuthError` ends the run as
  `interrupted` (and drops the expired connection).
- `execute_grade_plan` never re-sends an uncertain row (`sending`,
  `unknown`, `accepted_unverified`, `score_mismatch`);
  `resume_operation` re-reads it: planned grade and score present →
  verified; still the planned-from snapshot → sent once (`resumed_from`);
  anything else → stale. These re-reads send nothing, so they do not stop
  the other rows (the run still ends `needs_review`); a new problem on a
  row resume sends does. Stale rows are skipped by resume, re-checked by
  execute. Uncertain rows are processed first, one at a time, before any
  other row is dispatched. Cancellation is permanent for an operation.
- Results are compact (`attention_rows`, capped at 20, plus
  `attention_omitted`); `verbose=True` returns full rows. Question writes use shared thread/filesystem locks, requiring a valid private cache and coordinating only processes using that root. Pre-submission rubrics use AssignmentRubric props. Plan preparation and new archives are non-idempotent local operations. Autograder adapters are capability-gated to discovered forms; live programming-assignment validation remains pending. See README lifecycle matrix for supported boundaries.
