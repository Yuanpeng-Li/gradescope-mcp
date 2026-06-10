# Gradescope MCP Server Developer Guide

## Project Objective

This project exposes Gradescope course-management and grading capabilities
through FastMCP so that MCP clients can inspect courses, plan grading, review
student work, and execute carefully gated write operations.

The codebase is optimized for real-world instructor and TA workflows, especially
for scanned exams and AI-assisted grading where the client needs more than just
simple CRUD wrappers.

## Current Snapshot

- 34 tools
- 3 resources
- 7 prompts
- 30 automated tests
- Python 3.10+
- `uv` + `hatchling`
- `mcp` FastMCP server
- `gradescopeapi` plus direct HTTP/HTML/JSON scraping for unsupported cases

## Repository Map

### Runtime entry
- `src/gradescope_mcp/__main__.py`
  Loads `.env`, configures logging, and starts the server.
- `scripts/export_sso_cookie.py`
  Optional local helper that opens Chrome for school SSO and writes a
  Gradescope cookie header to `.env`.

### Server registration
- `src/gradescope_mcp/server.py`
  The authoritative tool/resource/prompt inventory. If counts in docs disagree,
  trust this file.

### Auth and session reuse
- `src/gradescope_mcp/auth.py`
  Maintains a singleton `GSConnection` and reuses the authenticated session.

### Tool modules
- `src/gradescope_mcp/tools/courses.py`
  Course listing and custom roster parsing.
- `src/gradescope_mcp/tools/assignments.py`
  Assignment listing, detail reads, date edits, rename.
- `src/gradescope_mcp/tools/submissions.py`
  Uploads, submission listing, per-student submission reads, grader discovery.
- `src/gradescope_mcp/tools/extensions.py`
  Extension reads and writes.
- `src/gradescope_mcp/tools/grading.py`
  Outline parsing, score export, grading progress.
- `src/gradescope_mcp/tools/grading_ops.py`
  Submission grading context, grade writes, rubric CRUD, question-submission
  discovery, navigation.
- `src/gradescope_mcp/tools/grading_workflow.py`
  Workflow helpers that save artifacts to `/tmp/gradescope-mcp`, compute readiness, cache
  pages, and build crop-first read plans.
- `src/gradescope_mcp/tools/answer_groups.py`
  AI-assisted answer-group inspection and batch grading.
- `src/gradescope_mcp/tools/regrades.py`
  Regrade list/detail scraping.
- `src/gradescope_mcp/tools/statistics.py`
  Assignment statistics.
- `src/gradescope_mcp/tools/safety.py`
  Shared confirmation-preview helper for mutations.

## Tool Counts

### Read / discovery
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
11. `tool_get_grading_progress`
12. `tool_get_regrade_requests`
13. `tool_get_regrade_detail`
14. `tool_get_assignment_statistics`
15. `tool_get_submission_grading_context`
16. `tool_get_question_rubric`
17. `tool_list_question_submissions`
18. `tool_get_next_ungraded`
19. `tool_get_answer_groups`
20. `tool_get_answer_group_detail`
21. `tool_prepare_grading_artifact`
22. `tool_assess_submission_readiness`
23. `tool_cache_relevant_pages`
24. `tool_prepare_answer_key`
25. `tool_smart_read_submission`

### Write / mutation
26. `tool_upload_submission`
27. `tool_set_extension`
28. `tool_modify_assignment_dates`
29. `tool_rename_assignment`
30. `tool_apply_grade`
31. `tool_create_rubric_item`
32. `tool_update_rubric_item`
33. `tool_delete_rubric_item`
34. `tool_grade_answer_group`

## Operating Assumptions

### Authentication
- Direct-login credentials can come from `GRADESCOPE_EMAIL` and
  `GRADESCOPE_PASSWORD`
- SSO / school-credential sessions can come from `GRADESCOPE_COOKIE_HEADER` or
  `GRADESCOPE_SESSION_COOKIE`
- Never hardcode credentials
- `python -m gradescope_mcp` automatically loads `.env`

### Write safety
- Every mutating tool is preview-first
- `confirm_write=False` must remain a no-op preview path
- `confirm_write=True` is required for actual mutation
- Rubric updates and deletions are cascading operations
- Batch answer-group writes can affect many submissions at once

### ID semantics
- Assignment-level submission listings return Global Submission IDs
- Grading operations need Question Submission IDs
- If a grading call returns 404, suspect the wrong ID type first

### Scoring semantics
- Gradescope questions can be `positive` or `negative`
- Rubric weights remain positive in both modes
- The scoring mode determines whether checked items add or deduct points

### Scanned assignment behavior
- Missing structured answer keys are common and expected
- Workflow helpers deliberately fall back to rubric + prompt + page evidence
- Page images and cached artifacts are written to `/tmp/gradescope-mcp`

## Testing And Data Hygiene

- Tests run against mocked/unit-style scenarios, but the project is designed for
  real Gradescope data and can touch sensitive student information during manual
  validation.
- If you perform real-account mutation tests, record them in
  `OPERATIONS_LOGS/RECORDS.md`.
- Do not include student names, IDs, grades, or raw submissions in repository
  logs.

Current test files:
- `tests/test_write_safety.py`
- `tests/test_grading_workflow.py`
- `tests/test_answer_groups.py`
- `tests/test_assignments_and_grading_ops.py`
- `tests/test_extensions_and_answer_key.py`

## Commands

```bash
uv run python -m gradescope_mcp
uv run pytest -q
npx @modelcontextprotocol/inspector uv run python -m gradescope_mcp
```

## Known Caveats

1. Several endpoints are reverse-engineered and can break if Gradescope changes
   its frontend payloads.
2. `courses.py` uses a custom roster parser because upstream parsing is not
   reliable with sections.
3. Some assignment types return 401 for extension APIs even for staff users.
4. `get_next_ungraded` and grading navigation need to guard against Gradescope
   self-loop behavior.
5. `/tmp/gradescope-mcp` artifacts are cache files, not durable project state.

## Maintenance Rule

When code changes alter capabilities, update `README.md`, this file, and
`DEVLOG.md` in the same change. The previous drift in tool counts and test counts
came from missing that step.
