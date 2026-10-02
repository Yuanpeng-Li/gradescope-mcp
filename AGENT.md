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

- 38 tools (24 read-only, 11 Gradescope writes, 3 local cache writes)
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
  Loads `.env`, calls `cache.configure_process_cache_env()` (an unsafe cache
  root is logged, not fatal), configures logging, and starts the server.

### Server registration
- `src/gradescope_mcp/server.py`
  The authoritative tool/resource/prompt inventory. If counts in docs
  disagree, trust this file (and `tests/test_docs_consistency.py` will fail).
  - `gs_tool(annotations)` registers a tool: it wraps the function in
    `with_session_recovery`, then `_signal_errors` (handled-failure text
    becomes `isError: true`; an escaped `AuthError` becomes an
    `Authentication error: ...` result), and registers it with the
    annotations, a title and `structured_output=False`.
  - Annotation helpers: `read_only(title)`, `gradescope_write(title,
    idempotent=...)` and `local_cache_write(title)`.
  - `gs_resource(uri)` registers a resource with session recovery; failure
    text is raised as `ResourceError`. Template IDs are validated with
    `_resource_id` (`ResourceNotFoundError` otherwise).
  - Argument types: `GradescopeID` (digits only; JSON numbers accepted,
    whitespace and backticks stripped), `OptionalGradescopeID` (blank or
    null means not given), `OutputFormat`, and the strict `GradeRow`
    TypedDict for `tool_apply_grade_batch`.
  - The prompts. They make no Gradescope requests and are registered with a
    plain `@mcp.prompt()`.

### Auth and session reuse
- `src/gradescope_mcp/auth.py`
  - `get_connection()` returns one process-wide `GSConnection`, created and
    dropped under a lock. The module logs in itself: credentials are POSTed
    as a form body, never put in a URL, and scrubbed from every error and
    log line. A rejected login is cached by credential fingerprint, so
    later calls fail fast with `INVALID_CREDENTIALS_MESSAGE` until the env
    credentials change.
  - `TimeoutHTTPAdapter` is mounted on the session for http and https and
    applies `DEFAULT_TIMEOUT` (10 s connect, 60 s read) to every request
    without its own timeout, including gradescopeapi's requests.
    `GRADESCOPE_MCP_HTTP_TIMEOUT` sets the read timeout and caps the connect
    timeout.
  - After login, a response hook raises `SessionExpiredError` (an
    `AuthError`) on a redirect to `/login` or `/account/auth` (before the
    redirect is followed), on the login page itself, or on a 401 "must be
    logged in", and flags the current thread.
  - `with_session_recovery(fn)` (applied by `gs_tool` / `gs_resource`) sees
    the flag, calls `reset_connection(expired=<that connection>)` and re-runs
    the call once on a fresh login. A second expiry returns
    `SESSION_RECOVERY_FAILED_MESSAGE`. `reset_connection()` without
    `expired` also does a best-effort logout.
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
  autograder image. Also `parse_date_input` / `check_date_order`, which
  `extensions.py` reuses.
- `src/gradescope_mcp/tools/submissions.py`
  Uploads (path vetting, `GRADESCOPE_MCP_UPLOAD_ROOT`), submission listing,
  per-student submission reads, grader discovery.
- `src/gradescope_mcp/tools/extensions.py`
  Extension reads and writes (course timezone resolution).
- `src/gradescope_mcp/tools/grading.py`
  Outline parsing, score export, grading progress, student submission links.
- `src/gradescope_mcp/tools/grading_ops.py`
  Submission grading context, grade writes (single and batch), confidence
  thresholds (`CONFIDENCE_REJECT_BELOW`, `CONFIDENCE_REVIEW_UP_TO`), rubric
  CRUD, question-submission discovery, navigation.
- `src/gradescope_mcp/tools/grading_workflow.py`
  Workflow helpers that write artifacts to the private cache, compute
  readiness (pre-read context, not grading confidence), cache pages, and
  build crop-first read plans.
- `src/gradescope_mcp/tools/answer_groups.py`
  AI-assisted answer-group inspection and batch grading.
- `src/gradescope_mcp/tools/regrades.py`
  Regrade list/detail scraping.
- `src/gradescope_mcp/tools/statistics.py`
  Assignment statistics.
- `src/gradescope_mcp/tools/common.py`
  Shared helpers: `normalize_rubric_ids`, `split_known_rubric_ids`,
  `format_untrusted`, `escape_md_cell`, `normalize_url`,
  `is_placeholder_page`, `select_crop_pages`, `page_number`.
- `src/gradescope_mcp/tools/safety.py`
  Shared confirmation-preview helper for mutations.

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

### Gradescope writes
`destructiveHint=true`, exactly the tools with `confirm_write`.
`idempotentHint=false` for upload and rubric-item creation, true otherwise.

25. `tool_upload_submission`
26. `tool_set_extension`
27. `tool_modify_assignment_dates`
28. `tool_rename_assignment`
29. `tool_update_autograder_image`
30. `tool_apply_grade`
31. `tool_apply_grade_batch`
32. `tool_create_rubric_item`
33. `tool_update_rubric_item`
34. `tool_delete_rubric_item`
35. `tool_grade_answer_group`

### Local cache writes
`readOnlyHint=false`, `destructiveHint=false`: they read Gradescope and
write only to the private cache.

36. `tool_prepare_grading_artifact`
37. `tool_cache_relevant_pages`
38. `tool_prepare_answer_key`

All tools are `openWorldHint=true`.

## Rules For Changing Tools

- Register new tools with `@gs_tool(<annotation helper>)` and new resources
  with `@gs_resource(uri)` in `server.py`, never with a bare `@mcp.tool()` /
  `@mcp.resource()`: those would skip session recovery, `isError` mapping
  and annotations.
- Type every Gradescope ID parameter as `GradescopeID` (or
  `OptionalGradescopeID`), and use `Literal` enums for fixed choices.
- A tool that changes Gradescope data takes `confirm_write: bool = False`,
  uses `gradescope_write(...)`, validates everything before the confirm
  gate, previews exactly what it will send (`tools/safety.py`), and reads
  the result back where practical. Its docstring says that `confirm_write`
  is not user approval.
- Handled failures return text starting with `Error:` (`Authentication
  error:` for `AuthError`, `❌` for a write Gradescope rejected). Previews,
  confidence rejections, `⚠️` warnings and "No ... found" messages must not
  use those prefixes.
- Wrap student-authored text with `format_untrusted`, and escape user or
  student data in markdown table cells with `escape_md_cell`.
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
- `python -m gradescope_mcp` loads `.env` with python-dotenv, searching
  upward from the package directory (not the working directory); variables
  already in the environment win
- After a rejected login, fix the credentials and restart the server

### Write safety
- Every mutating tool is preview-first
- `confirm_write=False` must remain a no-op preview path
- `confirm_write=True` is required for actual mutation, and is not human
  approval: prompts and the skill require a shown preview and explicit
  user approval first
- Rubric updates and deletions are cascading operations
- Batch answer-group writes can affect many submissions at once, including
  inferred members; already-graded members need `overwrite_graded=True`

### Error signalling
- `Error...`, `Authentication error...` and `❌...` results are returned with
  `isError: true` and unchanged text
- Schema validation failures come back as
  `Error executing tool <name>: ... validation error ...` with `isError: true`
- Tools publish no `outputSchema` and return text only

### ID semantics
- All IDs are digit strings (numbers accepted at the MCP layer)
- Assignment-level submission listings return Global Submission IDs
- Grading operations need Question Submission IDs
- If a grading call returns 404, suspect the wrong ID type first

### Scoring semantics
- Gradescope questions can be `positive` or `negative`
- Rubric weights remain positive in both modes; negative weights need
  `allow_negative=True`
- The scoring mode determines whether checked items add or deduct points
- `CONFIDENCE_REJECT_BELOW` (0.6) and `CONFIDENCE_REVIEW_UP_TO` (0.8):
  below 0.6 nothing is written; 0.6 to 0.8 inclusive is written and flagged
  NEEDS HUMAN REVIEW

### Dates
- Inputs are `YYYY-MM-DDTHH:MM` with an explicit time
- Assignment dates are course-local wall-clock times without an offset;
  omitted dates and the late-submission flag are preserved
- Extension dates without an offset use the course timezone (or the
  `timezone` argument); dates with an offset are absolute

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
- `tests/test_auth.py`
- `tests/test_common.py`
- `tests/test_dates_extensions_submissions.py`
- `tests/test_docs_consistency.py`
- `tests/test_extensions_and_answer_key.py`
- `tests/test_grading_ops_fixes.py`
- `tests/test_grading_workflow.py`
- `tests/test_p0_fixes.py`
- `tests/test_page_selection.py`
- `tests/test_read_side_fixes.py`
- `tests/test_server_mcp.py`
- `tests/test_session_recovery.py`
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
6. `tool_get_assignment_details` returns "Assignment ... not found" as an
   ordinary result, not an error.

## Maintenance Rule

When code changes alter capabilities, update `README.md`, this file,
`skills/gradescope-assisted-grading/SKILL.md` and `DEVLOG.md` in the same
change. `tests/test_docs_consistency.py` catches inventory and count drift,
but not wording; check the prose against the code.
