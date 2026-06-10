# Gradescope MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for
[Gradescope](https://www.gradescope.com/) that exposes course management,
grading, regrade review, statistics, and AI-assisted grading workflows to MCP
clients.

The server is designed for instructors and TAs who want to use AI agents with
real Gradescope data while keeping write operations gated behind explicit
confirmation.

This repository also includes a reusable local skill at
`skills/gradescope-assisted-grading/SKILL.md` for human-approved grading
workflows.

## Current Status

- 39 MCP tools
- 3 MCP resources
- 7 MCP prompts
- 76 automated tests
- Python 3.10+
- Package manager: `uv`

## What The Project Provides

### Read-oriented workflows
- Course discovery and assignment listing
- Assignment outline parsing for online and scanned-PDF assignments
- Roster inspection with a custom HTML parser
- Submission listing for multiple assignment types
- Staff upload-form inspection for student-targeted uploads
- Grading progress, rubric context, answer groups, regrades, and statistics
- Workflow helpers that cache grading artifacts and answer-key snapshots to
  `/tmp/gradescope-mcp`

### Write-oriented workflows
- Uploading submissions
- Uploading or replacing submissions on behalf of a student
- Setting student extensions
- Modifying assignment dates
- Renaming assignments
- Applying grades
- Creating, updating, and deleting rubric items
- Batch grading answer groups

All write-capable tools are preview-first and require `confirm_write=True`
before any mutation is executed.

## Tool Inventory

### Core
| Tool | Description | Access |
|------|-------------|--------|
| `tool_list_courses` | List all courses grouped by role | All |
| `tool_get_assignments` | List assignments for a course | All |
| `tool_get_assignment_details` | Get one assignment's details | All |
| `tool_upload_submission` | Upload files to an assignment | All |
| `tool_inspect_submission_upload_form` | Inspect staff upload forms before targeted uploads | Instructor/TA |
| `tool_upload_submission_for_student` | Upload or replace files on behalf of one student | Instructor/TA |

### Instructor / TA Management
| Tool | Description |
|------|-------------|
| `tool_get_course_roster` | Full roster grouped by role |
| `tool_get_extensions` | View assignment extensions |
| `tool_set_extension` | Add or update one student's extension |
| `tool_modify_assignment_dates` | Change release / due / late-due dates |
| `tool_rename_assignment` | Rename an assignment |
| `tool_get_assignment_submissions` | List assignment submissions |
| `tool_get_student_submission` | Read one student's submission content |
| `tool_get_assignment_graders` | View graders for a question |

### Grading Read
| Tool | Description |
|------|-------------|
| `tool_get_assignment_outline` | Question hierarchy, IDs, weights, prompt text |
| `tool_export_assignment_scores` | Assignment score export and summary |
| `tool_get_grading_progress` | Per-question grading dashboard |
| `tool_get_submission_grading_context` | Full grading context for a question submission |
| `tool_get_question_rubric` | Rubric inspection without a submission ID |
| `tool_list_question_submissions` | List Question Submission IDs, filterable by grade state |
| `tool_get_next_ungraded` | Navigate to the next ungraded question submission |

### Grading Write
| Tool | Description |
|------|-------------|
| `tool_apply_grade` | Apply rubric items, comments, and point adjustments |
| `tool_create_rubric_item` | Create a rubric item |
| `tool_update_rubric_item` | Update a rubric item |
| `tool_delete_rubric_item` | Delete a rubric item |

### AI-Assisted / Workflow Helpers
| Tool | Description |
|------|-------------|
| `tool_prepare_grading_artifact` | Save a question-specific grading artifact to `/tmp/gradescope-mcp` |
| `tool_assess_submission_readiness` | Estimate whether auto-grading is safe enough to attempt |
| `tool_cache_relevant_pages` | Download crop and nearby pages to `/tmp/gradescope-mcp` |
| `tool_prepare_answer_key` | Save assignment-wide answer-key notes to `/tmp/gradescope-mcp` |
| `tool_smart_read_submission` | Return a crop-first reading plan |

### Answer Groups
| Tool | Description |
|------|-------------|
| `tool_get_answer_groups` | List AI-clustered answer groups |
| `tool_get_answer_group_detail` | Inspect one answer group |
| `tool_grade_answer_group` | Batch-grade one answer group |

### Regrades
| Tool | Description |
|------|-------------|
| `tool_get_regrade_requests` | List regrade requests |
| `tool_get_regrade_detail` | Inspect one regrade request |

### Statistics
| Tool | Description |
|------|-------------|
| `tool_get_assignment_statistics` | Assignment-level and per-question statistics |

## Resources

| URI | Description |
|-----|-------------|
| `gradescope://courses` | Current course list |
| `gradescope://courses/{course_id}/assignments` | Assignment list for a course |
| `gradescope://courses/{course_id}/roster` | Roster for a course |

## Prompts

| Prompt | Description |
|--------|-------------|
| `summarize_course_progress` | Summarize assignment status in a course |
| `manage_extensions_workflow` | Guide extension-management work |
| `check_submission_stats` | Summarize assignment submission status |
| `generate_rubric_from_outline` | Draft a rubric from assignment structure |
| `grade_submission_with_rubric` | Walk through grading one student's work |
| `review_regrade_requests` | Review pending regrade requests |
| `auto_grade_question` | Run a confidence-gated grading workflow for one question |

## Architecture

### Entry points
- `src/gradescope_mcp/__main__.py`: loads `.env`, configures logging, runs the
  FastMCP server
- `src/gradescope_mcp/server.py`: registers all tools, resources, and prompts

### Authentication
- `src/gradescope_mcp/auth.py`: maintains a singleton `GSConnection`
- Direct Gradescope login uses `GRADESCOPE_EMAIL` and `GRADESCOPE_PASSWORD`
- SSO / school-credential login uses `GRADESCOPE_COOKIE_HEADER` exported from
  an authenticated browser session
- `.env` is loaded automatically when starting with `python -m gradescope_mcp`

### Tool modules
- `tools/courses.py`: course listing and roster parsing
- `tools/assignments.py`: assignment listing and assignment write operations
- `tools/submissions.py`: uploads, submission listing, grader discovery
- `tools/extensions.py`: extension reads and writes
- `tools/grading.py`: outline parsing, score exports, grading progress
- `tools/grading_ops.py`: grading context, writes, rubric CRUD, navigation
- `tools/grading_workflow.py`: `/tmp/gradescope-mcp` artifacts, answer keys, readiness, page
  caching, smart reading
- `tools/answer_groups.py`: AI-assisted answer-group inspection and batch writes
- `tools/regrades.py`: regrade listing and detail inspection
- `tools/statistics.py`: assignment statistics
- `tools/safety.py`: preview-first confirmation helpers for mutations

## Important Behavior And Constraints

### Write safety
- Mutating tools return a preview when `confirm_write=False`
- The actual change only happens with `confirm_write=True`
- Rubric edits and deletions can cascade to existing grades
- `tool_grade_answer_group` can affect many submissions at once and needs extra
  care

### Submission IDs
- `tool_get_assignment_submissions` returns assignment-level Global Submission
  IDs
- Grading tools require Question Submission IDs
- Use `tool_list_question_submissions`, `tool_get_next_ungraded`, or grading
  context tools to get the correct IDs

### Scoring direction
- Gradescope questions may be `positive` or `negative` scoring
- Rubric weights are stored as positive numbers in both modes
- The scoring mode determines whether a checked rubric item adds or deducts
  points

### Scanned / handwritten assignments
- Structured reference answers are often unavailable
- This is expected, not necessarily a parsing failure
- The workflow helpers are built to use crop regions, full pages, adjacent pages,
  rubric text, and user-provided reference notes

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

Then edit `.env` with either:

```env
# Preferred for SSO / school-credential users.
GRADESCOPE_COOKIE_HEADER=_gradescope_session=...
```

For SSO users, the optional helper can open Chrome and write this value after
you complete normal school login:

```bash
python3 scripts/export_sso_cookie.py --url https://www.gradescope.com/
```

The helper waits for you to confirm that school SSO is complete before saving
cookies. For non-SSO flows, `--auto-detect` can restore automatic saving.

or:

```env
# Direct Gradescope-account users only.
GRADESCOPE_EMAIL=your_email@example.com
GRADESCOPE_PASSWORD=your_password
```

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
        "GRADESCOPE_COOKIE_HEADER": "_gradescope_session=..."
      }
    }
  }
}
```

### 5. Debug with MCP Inspector
```bash
npx @modelcontextprotocol/inspector uv run python -m gradescope_mcp
```

### 6. Run tests
```bash
uv run pytest -q
```

## Assisted Grading Skill

The repository includes one project-local skill:

- `gradescope-assisted-grading`

It is intended for:
- preview-first grading
- rubric review before mutation
- scanned exam grading
- answer-group triage
- explicit human approval before any grade write

### Install the skill locally
```bash
mkdir -p /tmp/gradescope-mcp/skills
ln -s "$(pwd)/skills/gradescope-assisted-grading" /tmp/gradescope-mcp/skills/gradescope-assisted-grading
```

If you prefer copying:

```bash
mkdir -p /tmp/gradescope-mcp/skills
cp -R skills/gradescope-assisted-grading /tmp/gradescope-mcp/skills/
```

### Verify installation
```bash
ls /tmp/gradescope-mcp/skills/gradescope-assisted-grading
cat /tmp/gradescope-mcp/skills/gradescope-assisted-grading/SKILL.md
```

Invoke it from a client with:
- `Use the gradescope-assisted-grading skill`
- `$gradescope-assisted-grading`

## Project Structure

```text
gradescope-mcp/
├── .env.example
├── AGENT.md
├── DEVLOG.md
├── OPERATIONS_LOGS/
│   └── RECORDS.md
├── README.md
├── pyproject.toml
├── skills/
│   └── gradescope-assisted-grading/
│       └── SKILL.md
├── src/
│   └── gradescope_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── auth.py
│       ├── server.py
│       └── tools/
│           ├── __init__.py
│           ├── answer_groups.py
│           ├── assignments.py
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
    ├── test_answer_groups.py
    ├── test_assignments_and_grading_ops.py
    ├── test_extensions_and_answer_key.py
    ├── test_grading_workflow.py
    └── test_write_safety.py
```

## Development Notes

- `AGENT.md` summarizes the current architecture and maintenance expectations
- `DEVLOG.md` records the implementation history
- `OPERATIONS_LOGS/RECORDS.md` is the mutation log template for real-account
  testing

## Known Caveats

1. Gradescope behavior differs across assignment types; several tools rely on
   HTML parsing or reverse-engineered endpoints.
2. Roster parsing uses a custom parser because the upstream library parser is
   unreliable when sections are present.
3. Some assignment types do not support the extensions API even for staff users.
4. Scanned assignments usually do not provide a structured answer key.
5. Question grading requires Question Submission IDs, not assignment-level Global
   Submission IDs.
