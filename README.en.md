<h1 align="center">Bandi · Your Personal Multi-Agent Digital-Employee Org</h1>

<p align="center"><i>Run your AI employees like a company: roles, SOPs, skills, models — assigned by project.</i></p>

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/team-group-chat.png" alt="Bandi team group chat: several AI employees collaborating in one conversation" width="100%" />
</p>

<p align="center">
  <b>English</b> · <a href="README.md">简体中文</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat" /></a>
  <a href="bandi/pyproject.toml"><img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" /></a>
  <a href="frontend/package.json"><img alt="node" src="https://img.shields.io/badge/node-20%2B-339933?style=flat&logo=node.js&logoColor=white" /></a>
  <a href="frontend/package.json"><img alt="react" src="https://img.shields.io/badge/React-18-61DAFB?style=flat&logo=react&logoColor=white" /></a>
  <a href="LICENSE"><img alt="local first" src="https://img.shields.io/badge/local--first-BYKO-black?style=flat" /></a>
</p>

---

## What is Bandi

Bandi is an **organization-first personal AI employee framework**. It treats AI as a company you run, not as a chat box:

- **Roles** — market research, PM, frontend, backend, QA, support… each employee has its own responsibilities, reporting line, delegation scope, and model profile.
- **SOPs & skills** — write "how the work gets done" as a visual SOP plus reusable skill cards, so employees follow a process instead of improvising every session.
- **Project-based assignment** — create a project, break it into work items, dispatch them to employees; work flows downstream between members.
- **Memory & knowledge** — employees remember preferences, conclusions, and lessons; anything headed for long-term memory waits for your approval.
- **In-org collaboration** — direct messages, team group chats, @mentions, and handoff summaries; employees actually hand work to each other.

Everything lives in `~/.bandi/` on your machine. Both ports bind to `127.0.0.1` only, and model keys are yours (BYOK).

## How it differs from a chatbot

| The usual setup | Bandi |
|---|---|
| One chat box, one answer at a time | One organization: several employees with their own scope, work flowing between them |
| Re-explain the context every session | Employees remember preferences, conclusions and lessons; long-term memory needs your approval |
| Prompts hardcoded in source | Duties, SOPs, skills and models are configuration — change the config, change the employee |
| One model does everything | Each employee can bind a different model: cheap ones for grunt work, strong ones for decisions |

## Typical uses

- **Solo product work**: PM splits the brief → design drafts → frontend and backend in parallel → QA signs off; you only approve at checkpoints.
- **Project-by-project delivery**: create work items per project and assign them; roles and skills are reused across projects.
- **Scheduled operations**: scheduled jobs let employees run research, write digests and watch metrics on their own, with output landing on the project board.

---

## Product tour

<table>
<tr>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/team.png" alt="Team page: members, roles and work" /><br/>
<sub><b>Team</b> — see who works here, who reports to whom, and what everyone is doing.</sub>
</td>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/chat.png" alt="Direct chat with one AI employee" /><br/>
<sub><b>Direct chat</b> — hand work to one employee; model, skills and SOP follow that role's config.</sub>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/employee-editor.png" alt="Employee editor: duties, SOP, skills and model" /><br/>
<sub><b>Employee editor</b> — duties, tool surface, SOP, skill bindings and model tier in one panel.</sub>
</td>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/org-canvas.png" alt="Org canvas: drag-and-drop org chart" /><br/>
<sub><b>Org canvas</b> — build the org chart on a canvas: reporting lines and delegation, by dragging.</sub>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/memory-approval.png" alt="Memory approval before writing to long-term memory" /><br/>
<sub><b>Memory approval</b> — you see the distilled conclusion first; nothing enters long-term memory without a nod.</sub>
</td>
<td width="50%" valign="top">
<img src="https://cdn.jsdelivr.net/gh/liujunyi106-cyber/bandi@main/assets/screenshots/ontology.png" alt="Ontology: the knowledge organization layer" /><br/>
<sub><b>Knowledge layer</b> — entities, relations and assertions become a queryable ontology, not loose transcripts.</sub>
</td>
</tr>
</table>

---

## Highlights

- 🏢 **An org runtime, not another chat box** — one turn kernel drives direct chat, group chat, background jobs and sub-agents; every employee has its own duties, tool allowlist, skills, SOP and model tier.
- 🧩 **Project-based scheduling** — create projects, split work items, dispatch along dependency chains; concurrency gates, leases, retries and close-out live in the scheduler, you approve at checkpoints.
- 🧠 **Layered memory** — episodic / long-term / team / Markdown, with time decay and usage-based revival; low-value noise is gated out and anything important enters memory only after your approval.
- 🔐 **Permissions & sandbox** — layered rules (deny / ask / bypass), path allowlists, command blacklist, risk-level interception; writes and shell commands can require a second confirmation.
- 🔍 **Observability** — every tool call, reasoning step and token is accounted for; trajectory replay plus usage and cost stats.
- 🧱 **Skills & SOPs** — reusable skill cards plus a visual SOP editor (add/remove nodes, wire edges, save bumps a patch version).
- 📚 **Knowledge layer** — documents are sectioned and chunked on ingest; concepts and citation chains are searchable and can be cited at the end of a reply.
- ⏱️ **Background scheduling** — scheduled and session-backed background runs keep work moving while you are away.
- 🌏 **Bilingual and local-first** — i18n across UI and API; data lives in `~/.bandi/`, models use your own keys.

## Tool catalog

91 built-in tools, grouped under `tools.feature_flags.tool_groups` (each group can be switched off).
Every call goes through one executor: permission check → sandbox → audit log.

| Group | Tools |
|---|---|
| Files & code | `read` `write` `edit` `apply_patch` `glob` `grep` `list_dir` `bash` `python_exec` `git_status` `git_diff` `git_commit` |
| Web & search | `web_search` `web_fetch` `web_scraper` `web_login` `web_image_search` `web_image_download` `browser` `tool_search` |
| Documents | `read_document` `word_reader` `pdf_reader` `excel_reader` `image_ocr` `doc_classifier` `archive_manager` `markdown_processor` `html_processor` `text_processor` `csv_processor` `config_file` |
| Papers & citations | `pdf_structure` `citation_extractor` `section_summarizer` `pdf_translate` |
| Generation | `document_generate` `excel_generate` `slides_generate` `checklist_generate` `template_filler` `document_template` `theme_designer` |
| Charts & media | `chart_generator` `image_generate` `video_generate` `audio_generate` |
| Canvas / GenUI | `emit_ui_tree` `list_ui_components` `canvas_publish` `get_genui_guide` `get_html_canvas_guide` |
| Knowledge base | `knowledge_ingest` `knowledge_search` |
| Memory & ontology | `memory_search` `memory_read` `memory_forget` `memory_assert` |
| Org collaboration | `inbox` `send_dm` `reply_message` `ask_peer_and_wait` `broadcast_issue` `start_meeting` `respond_meeting` `route_work` `close_human_review` `team_list` `team_create` `spawn_thread_task` |
| Board management | `manager_board_read` `modify_work_item` `delete_work_item` |
| Skills & flows | `skill_invoke` `skill_discover` `workflow_list` `workflow_run` `workflow_status` `workflow_pause` `workflow_resume` `workflow_cancel` `workflow_save` `workflow_embed_emit` `chat_workflow` |
| Interaction & state | `request_user_input` `todo_read` `todo_write` `exit_plan_mode` `resource_configure` `ctx_inspect` |

---

## Quick start

> The goal is a **front end and back end you can just open** — no CLI needed to get work done. These commands only start the services.

### Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Backend kernel (FastAPI) |
| Node.js | 20+ | Frontend (Vite + React) |
| pnpm | 10+ | Frontend package manager (exact pin in `frontend/package.json` → `packageManager`) |

### Three steps

```bash
# 1) Backend dependencies
cd bandi
python -m venv .venv
source .venv/bin/activate      # macOS/Linux; on Windows use .venv/Scripts/activate
pip install -e ".[dev]"

# 2) Frontend dependencies
cd ../frontend
pnpm install

# 3) Start the dev server (it launches the backend for you)
pnpm run dev
```

Open **http://127.0.0.1:5173** and you are in.

When `pnpm run dev` starts, a Vite plugin boots the backend kernel from `bandi/.venv` on
`127.0.0.1:8766`, waits for a health check, and reuses an already-healthy kernel.
Stopping the dev server stops the kernel.

### Ports

| Service | Address | Notes |
|---|---|---|
| Frontend (Vite) | `127.0.0.1:5173` | The page you open |
| Backend kernel (FastAPI) | `127.0.0.1:8766` | `/api` and `/ws` are proxied here |

Both bind to loopback only — nothing is exposed to your LAN.

### Desktop window (optional)

```bash
cd frontend
pnpm run electron:compile   # compile the main process
pnpm run dev:desktop        # vite + uvicorn + Electron
```

Packaging config lives in `frontend/electron-builder.yml`; run `electron-builder` when you want an installer.

### macOS / Linux

The auto-launcher uses the Windows interpreter path (`bandi/.venv/Scripts/python.exe`).
On other platforms, start the backend yourself:

```bash
cd bandi && .venv/bin/python -m uvicorn bandi.api.app:app --host 127.0.0.1 --port 8766
```

Then run `pnpm run dev` as usual — an already-running kernel is detected and reused.
Windows is the primary development platform today.

---

## Model configuration

Bandi ships no keys. Pick one of three paths:

1. **In the UI (recommended)** — Settings → Models, enter `base_url` + `api_key` + model name, then switch models from the composer. Profiles are stored in `~/.bandi/llm_profiles.json`.
2. **Environment variables** — `BANDI_API_KEY` / `BANDI_BASE_URL` / `BANDI_MODEL`.
3. **Config file** — copy `bandi/config.yaml` to `~/.bandi/config.yaml` and set `llm.api_key` (the copy in the repo is a template and has no effect).

Built-in providers: OpenAI, DeepSeek, Anthropic, Gemini, Moonshot, Zhipu, MiniMax, Ollama, and any OpenAI-compatible endpoint.

**Bring your own gateway**: set provider to `openai_compat` and point `base_url` at your endpoint — no code changes.

On Windows you can also encrypt the key with `bandi/scripts/protect-secret.ps1 -Secret "sk-..."` and paste the
resulting `dpapi:<base64>` value as `api_key`; only the current user on that machine can decrypt it.

---

## Teams and role templates

The repo carries **templates only** — no personal team data. Your teams, employees and projects live in `~/.bandi/`.

Built-in templates live in [`bandi/bandi/company/team_template.py`](bandi/bandi/company/team_template.py):

| Template | Topology |
|---|---|
| Product development | PM → UI design / frontend / backend / QA |
| E-commerce selection | Selection lead → market research / supply chain / design / support |

Plus five org blueprints in [`bandi/bandi/company/architecture_registry.py`](bandi/bandi/company/architecture_registry.py):
Startup Studio, Enterprise Corporation, Creative Agency, Research Lab, DevOps Pipeline.

Once instantiated, add or remove roles, rewire reporting lines, swap models — templates are just a starting point.

---

## Architecture

```
┌────────────────────── browser / Electron shell ───────────────────┐
│  chat · team · project board · settings · resource center · SOP   │
└───────────────┬───────────────────────────────────────────────────┘
                │ /api (REST) · /ws (streaming)
                ▼
┌──────────────────── FastAPI kernel (127.0.0.1:8766) ───────────────┐
│  Agent loop / state machine / recovery   LLM registry · profiles  │
│  Company scheduling · work items · group chat   Memory (4 kinds)  │
│  Tools (files / search / data / media / browser)  Knowledge · Ontology │
│  Scheduler · observability · eval        Safety · approvals       │
└───────────────┬───────────────────────────────────────────────────┘
                │
                ▼
        local ~/.bandi/ (sessions, memory, teams, projects, credentials)
```

| Layer | Stack |
|---|---|
| Frontend | React 18 · TypeScript · Vite 6 · Tailwind CSS v4 · shadcn/ui · zustand |
| Desktop | Electron 33 (main process + preload + sidecar management) |
| Backend | Python 3.11+ · FastAPI · uvicorn · pydantic v2 · httpx |
| Storage | local files + SQLite (sessions / work items / checkpoints) |
| Models | in-house provider registry (OpenAI-compatible first), any BYOK gateway |

Layout:

```
bandi/                 # backend
├── bandi/             #   Python package (agent / api / company / memory / tools / safety …)
├── tests/             #   pytest: contract + unit + integration
├── scripts/           #   dev scripts (verify, secret encryption, packaging)
└── config.yaml        #   config template (the live config is ~/.bandi/config.yaml)

frontend/              # frontend
├── src/               #   pages / components / state / services / i18n
├── electron/          #   Electron main process and preload
└── vite-backend-plugin.ts   # boots the backend kernel in dev
```

---

## Security model

- **No network exposure by default** — both ports bind to `127.0.0.1`.
- **Path sandbox** — file tools may only read and write inside allowed roots; anything else is refused.
- **Layered permissions** — `permissions` supports `deny_tools` / `ask_tools` / `bypass_tools` / `deny_commands` plus risk-level interception.
- **Human approval** — writes, command execution and external calls can require confirmation; background jobs obey the same policy.
- **Key at rest** — DPAPI encryption on Windows; session records written to disk are scrubbed of common key shapes (`sk-*`, `Bearer` headers).
- **Memory approval** — candidate long-term memories are distilled and shown to you before they are committed.

> This is a local-first tool, not a multi-tenant service. Do not expose the API port to the internet.

---

## FAQ

**Do I have to use the CLI?**
No. Day-to-day work happens in the UI — build teams, configure employees, assign work, approve, inspect traces. The commands only start the services.

**Are my data and keys uploaded anywhere?**
No. Data lives in `~/.bandi/`, keys in `~/.bandi/llm_profiles.json` on your machine, and model requests go from your machine straight to the provider you configured.

**Does the repo ship the author's own teams or data?**
No. Only team templates and example configuration — no personal teams, sessions or memories.

**Can I use a local model?**
Yes. Ollama speaks the OpenAI-compatible protocol: pick the `ollama` provider, or point a local `base_url` in Settings.

---

## Contributing

Issues and PRs are welcome. Before submitting:

```bash
# backend
cd bandi && .venv/Scripts/python.exe -m pytest tests -q

# frontend
cd frontend && pnpm run lint && pnpm test
```

- Backend style: `ruff` (line-length 100), typed public functions.
- Frontend: TypeScript + ESLint + Prettier; when you change copy, update `src/i18n/{zh,en}.json`.

---

## License

[MIT](LICENSE) — free to use, modify, distribute and commercialize, as long as the copyright notice is kept.
