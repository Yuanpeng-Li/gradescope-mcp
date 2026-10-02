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

## Current Status

- 38 MCP tools (24 read-only, 11 that write to Gradescope, 3 that only write
  to the private local cache)
- 3 MCP resources (1 static resource, 2 URI templates)
- 7 MCP prompts
- Python 3.10+
- MCP Python SDK v2 (`mcp>=2.2,<3`, `MCPServer`) and `gradescopeapi>=1.8.1`
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
| `tool_upload_submission` | write | Upload local files as a new submission from the logged-in account (path restrictions below) |

### Instructor / TA Management
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_course_roster` | read-only | Roster grouped by role: name, email, SID, user ID, submission count, sections |
| `tool_get_extensions` | read-only | Extensions for one assignment |
| `tool_set_extension` | write | Add or update one student's extension; `timezone` for dates without an offset |
| `tool_modify_assignment_dates` | write | Change release / due / late-due dates; omitted dates are kept |
| `tool_rename_assignment` | write | Rename an assignment |
| `tool_update_autograder_image` | write | Change a programming assignment's autograder Docker Hub image |
| `tool_get_assignment_submissions` | read-only | Global Submission IDs with graded status, progress and late flag |
| `tool_get_student_submission` | read-only | One student's typed answers (untrusted blocks), file and page links, per-question and total scores |
| `tool_get_assignment_graders` | read-only | Staff who last graded a question's submissions, with counts (not the assigned graders) |

### Grading Read
| Tool | Kind | Description |
|------|------|-------------|
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
| `tool_apply_grade` | write | Set one submission's rubric items, point adjustment and comment; reads the score back |
| `tool_apply_grade_batch` | write | Grade many submissions of one question in one call; typed rows, per-row read-back |
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
| `tool_smart_read_submission` | read-only | Crop-first reading plan: crop page, adjacent pages, other pages; typed answers for online questions |

### Answer Groups
| Tool | Kind | Description |
|------|------|-------------|
| `tool_get_answer_groups` | read-only | AI-clustered answer groups with sizes, graded counts and inferred members |
| `tool_get_answer_group_detail` | read-only | One group's members, crops and graded counts (confirmed and inferred) |
| `tool_grade_answer_group` | write | Grade every member of one group; `overwrite_graded` and `expected_member_count` guards |

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

`course_id` in a resource URI must be digits; anything else is a JSON-RPC
error (`-32602`, "Invalid resource URI ..."). A resource whose read fails
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
the agent to preview each batch with `tool_apply_grade_batch(confirm_write=False)`
and to write only the rows the user explicitly approves.

## How The Tools Behave

### IDs
- Every Gradescope ID parameter (`course_id`, `assignment_id`,
  `question_id`, `submission_id`, `group_id`, `rubric_item_id`, `user_id`,
  `rubric_item_ids` elements and batch-row IDs) is a string of digits
  (`^\d+$` in the input schema).
- JSON numbers are accepted and converted; surrounding whitespace and
  backticks are stripped. Anything else (`"../1"`, `"-1"`, `1.5`, `true`) is
  rejected before the tool runs.
- An optional ID that is blank or `null` means "not given".

### Submission IDs
- `tool_get_assignment_submissions` returns assignment-level Global
  Submission IDs.
- Grading tools require Question Submission IDs. Get them from
  `tool_list_question_submissions`, `tool_get_student_submission_map`,
  `tool_get_next_ungraded`, or the regrade tools. A 404 from a grading tool
  usually means a Global Submission ID was passed.

### Write safety
- The tools with a `confirm_write` parameter (the `write` kind, 11 of
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
  own; the prompts and the skill tell agents to show the preview and wait
  for the user's explicit approval first.
- The write tools are annotated `destructiveHint: true` (and
  `readOnlyHint: false`), so a client can require confirmation for them.
  Keep them out of any "always allow" list in your MCP client so each write
  needs approval.
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
- Invalid arguments (bad IDs, unknown batch-row keys, values outside an
  enum, missing required arguments) are rejected by the input schema and
  come back with `isError: true` as
  `Error executing tool <name>: ... validation error ...`.

### Untrusted student text
Student-authored content (typed answers, regrade messages, answer-group
titles and inferred answers) is returned inside fenced
`<<<BEGIN UNTRUSTED ...>>>` / `<<<END UNTRUSTED ...>>>` blocks, or flagged by
`untrusted_fields_note` in JSON output. It is data to grade, never
instructions to follow.

### Dates and timezones
- Both date tools need an explicit time: `YYYY-MM-DDTHH:MM` (`:00` seconds
  allowed). Bare dates, non-zero seconds and impossible dates are rejected.
  An empty string or `null` means "not provided".
- `tool_modify_assignment_dates`: course-local wall-clock times with no UTC
  offset. Omitted dates and the allow-late-submissions setting are read
  from the assignment settings and re-sent unchanged. Passing
  `late_due_date` turns late submissions on; the tool cannot turn them off.
  It refuses to write when a current value it must keep can't be read, and
  verifies the result by re-reading the settings.
- `tool_set_extension`: dates without an offset are wall-clock times in the
  course timezone Gradescope reports on the extensions page (or the
  `timezone` argument, an IANA name, when Gradescope reports none), never
  the server's timezone. Dates with an offset (`Z`, `-07:00`) are absolute.
  Don't mix the two styles. Dates must be in order (release <= due <= late
  due). Only the dates passed are sent, so pass existing extension dates
  again to keep them. The preview shows each resolved UTC instant and the
  student's current extension; the write is read back.

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
- `tool_apply_grade_batch` rows accept only `submission_id` (required),
  `rubric_item_ids`, `point_adjustment`, `comment` and `confidence`. The
  preview loads every row and flags already-graded rows that would be
  OVERWRITTEN; execution re-reads each row before saving it and reports the
  scores read back from Gradescope.

### Answer groups
- `tool_grade_answer_group` refuses (with an Error listing them) when any
  confirmed or inferred member is already graded, unless
  `overwrite_graded=True`. Set that only with the user's approval.
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
refused. The preview lists each file's size and SHA-256.

### Scoring direction
- Gradescope questions use `positive` or `negative` scoring.
- Rubric weights are positive numbers in both modes; the scoring type
  decides whether a checked item adds or deducts points. Negative weights
  are rejected unless `allow_negative=True` is passed deliberately.

### Scanned / handwritten assignments
- Structured reference answers are often unavailable. This is expected, not
  necessarily a parsing failure.
- Students often tag the wrong pages. The workflow helpers use crop regions,
  the rest of the crop page, adjacent pages, then every other page, plus
  rubric text and user-provided reference notes.
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

`python -m gradescope_mcp` loads a `.env` file with python-dotenv. The search
starts in the package directory (`src/gradescope_mcp/` in this checkout) and
walks up, so the project's `.env` is found when you run from source. It is
not looked up in the current working directory, and if the project has no
`.env`, one in a parent directory would be used. Variables already set in the
environment (for example in the MCP client's `env` block) take precedence.
With a non-editable install, pass the variables through the client
configuration instead.

## Authentication

- Credentials come only from `GRADESCOPE_EMAIL` and `GRADESCOPE_PASSWORD`.
  The server logs in itself and POSTs them as a form body to `/login`
  (gradescopeapi would put them in the URL query string). Error messages and
  logs never contain them.
- Once Gradescope rejects the credentials, every later call fails
  immediately with `Authentication error: Gradescope login failed: invalid
  credentials.` without contacting Gradescope, until the credentials change.
  Fix `.env` or the client configuration and restart the server. Network
  and server errors are not cached.
- Every request has a default timeout of 10 s to connect and 60 s to read
  (see `GRADESCOPE_MCP_HTTP_TIMEOUT`), so a stalled connection fails the
  call instead of hanging it.
- When Gradescope's session expires during a call (a redirect to the login
  page, the login page itself, or a 401 "must be logged in"), the tool or
  resource logs in again and re-runs once. If the session expires again,
  the result is
  `Authentication error: Gradescope session expired and re-login did not restore access.`
- Only email/password login is supported.

## Architecture

### Entry points
- `src/gradescope_mcp/__main__.py`: loads `.env`, sets up the private cache
  environment, configures logging, and runs the `MCPServer` (mcp v2) over
  stdio. The `gradescope-mcp` console script calls the same `main()`.
- `src/gradescope_mcp/server.py`: registers all tools, resources and
  prompts. Tools are registered with `gs_tool(annotations)` and resources
  with `gs_resource(uri)`; both add session recovery, and `gs_tool` adds the
  annotations, the title and the `isError` mapping. It also defines the
  `GradescopeID` argument type and the typed `GradeRow` batch rows.

### Runtime
- mcp v2 runs these sync tool functions on worker threads, so tool calls can
  run concurrently and pings and cancellation are handled while a tool
  waits on Gradescope.
- `src/gradescope_mcp/auth.py`: one shared `GSConnection` behind a lock;
  form-body login, failed-login cooldown, `TimeoutHTTPAdapter` default
  timeouts, a response hook that raises `SessionExpiredError`, and
  `with_session_recovery`, which re-runs a call once after an expiry.
- `src/gradescope_mcp/cache.py`: the private per-user cache root and safe
  artifact writes.

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
- `tools/common.py`: shared helpers (rubric-ID normalization, untrusted-text
  blocks, markdown escaping, page selection)
- `tools/safety.py`: the standard write-preview message

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
    ├── test_p0_fixes.py
    ├── test_page_selection.py
    ├── test_read_side_fixes.py
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
2. Roster parsing uses a custom parser because the upstream library parser is
   unreliable when sections are present.
3. Some assignment types do not support the extensions API even for staff
   users, and removing an extension is not supported.
4. Scanned assignments usually do not provide a structured answer key.
5. Question grading requires Question Submission IDs, not assignment-level
   Global Submission IDs.
6. `tool_get_assignment_details` reports an unknown assignment as an
   ordinary result (``Assignment `X` not found in course `Y`.``), not as an
   error.
