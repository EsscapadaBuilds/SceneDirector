# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

Commonly used development commands:

| Command | Description |
|---|---|
| `python cli.py` | Launch the SceneDirector CLI interactively (pick/create project) |
| `python cli.py my_project` | Open an existing project or create it if missing |
| `python cli.py --doctor` | Run environment diagnostics (LLM, Blender, image services) |
| `python tests/smoke.py` | Run offline smoke tests (project setup, gates, mock 3D APIs) |
| `python tests/smoke.py --blender` | Run tests that also build a scene in Blender |
| `python tests/smoke.py --net` | Run tests that also call Poly Haven / Pollinations |
| `npm run app` (in `ui/`) | Launch the Tauri desktop app in development (starts the Python backend itself) |
| `npm run app:build` (in `ui/`) | Build the desktop installer |
| `python sd.py setup` | Install `requests`, download portable Blender into `tools/` if none is found, install the Claude skill to `~/.claude/skills` |
| `python sd.py <project> <tool> key=value ...` | Run one agent tool and print its JSON result (`python sd.py tools` lists them); used by the `scenedirector` skill |
| `python -m directorc.server` | Run the desktop backend alone on 127.0.0.1:8765 (for `npm run dev` in a browser) |

### CLI command reference (inside the running CLI)

```
/help                     show this help
/status                   project brief, stage and gates
/refs [open]              list reference images (and open the folder)
/assets [open]            list downloaded/imported assets
/inbox [import]           list files in assets/inbox (or register them)
/attach [kind] <path>     attach a reference image to your next message
/open <path>              open any file in default application
/approve <stage> [notes]  manually approve a gate
/new <name>               create and switch to a new project
/projects                 list projects
/doctor                   check LLM, Blender, network
/quit                     exit (progress is saved)
```

## High-level Architecture

**SceneDirector** is a conversational AI director for Blender. The overall flow is:

1. User describes a scene or character
2. Agent generates concept reference images via Pollinations.ai
3. User approves reference images (approval gates: `scene_reference`, `character_reference`)
4. Agent builds the scene in Blender headless via `bpy` Python API
5. Agent renders verification screenshots; user compares with references
6. Agent keyframes animation, renders preview → final still / MP4 animation
7. Optional: AI image-to-3D (Tripo/Meshy, credit-based) to import models

### Key modules

| Module | Purpose |
|---|---|
| `cli.py` | CLI entry point; argument parsing, project lifecycle, agent loop |
| `directorc/config.py` | `.env`-driven configuration (LLM endpoint/key/model, Blender path, image providers, rendering defaults) |
| `directorc/state.py` | `Project` class — directory layout, manifest.json, references, assets, approval gates, chat history |
| `directorc/agent/` | Conversation loop (`core.py`); tool definitions and execution (`tools.py`); system prompts (`prompts.py`) |
| `directorc/blender/bridge.py` | Headless Blender runner: `blender -b --python runtime.py -- payload.json`; probe, python execution, screenshot, render |
| `directorc/llm.py` | Provider-agnostic LLM client (Ollama, Groq, Gemini, OpenRouter); chat completions, token budgeting, retry on 429 |
| `directorc/ai3d.py` | Image-to-3D via Tripo/Meshy APIs (credit-based, paid); auto-poll + download + health check |
| `directorc/assets/providers.py` | Free asset search/download: Poly Haven CC0 (models/HDRI/textures), ambientCG CC0 materials, local inbox |
| `directorc/concept/images.py` | Concept image generation via Pollinations.ai (free, keyless; 1 image / 45s per IP) |
| `directorc/server.py` | Stdlib HTTP backend for the desktop app: project/media JSON API, file serving, and agent turns streamed as server-sent events. Wraps the same `Project`/`Agent`/`Bridge` as the CLI |
| `ui/src/` | React interface (Vite): `App.jsx` (projects, conversation, composer), `Panel.jsx` (media/assets/approvals, lightbox, diagnostics), `api.js` (backend client) |
| `ui/src-tauri/` | Tauri 2 shell (Rust): starts `python -m directorc.server` on a free port, exposes its URL to the UI, stops it on exit |

### Claude skill

- `.claude/skills/scenedirector/SKILL.md` lets Claude act as the director in chat, replacing the built-in LLM loop. It drives `sd.py` / `directorc/tool.py`, which runs a single `Toolbox` tool per process.
- The skill restates the workflow rules and Blender helper list from `directorc/agent/prompts.py`. When you change that prompt, a tool's arguments, or a helper in `directorc/blender/runtime.py`, update the skill too. `python sd.py setup` installs a copy at `~/.claude/skills/scenedirector/` with the absolute path filled in (`directorc/setup.py`, which must stay standard-library only); rerun it after editing the skill.

### Desktop app notes

- The UI never touches disk or Blender itself; everything goes through `directorc/server.py`. New features need an endpoint there first.
- In the app, `show_to_user` is overridden per session to show files inline instead of opening the OS viewer (see `Session._get_agent`).
- A turn's live items are published through `HUB` with increasing `seq`; `GET /api/projects/<name>` returns the `seq` it is consistent with, so the UI can drop older events.
- Allowed UI origins are listed in `ALLOWED_ORIGINS`; the CSP in `ui/src-tauri/tauri.conf.json` must allow `http://127.0.0.1:*` for the backend.

### Project layout (per project name under `projects/`)

```
projects/<name>/
  scene.blend          the Blender scene (source of truth)
  manifest.json        references, assets, licenses, gates, stage
  chat.json            conversation history
  references/          generated + uploaded reference images
  assets/              downloaded models / HDRIs / textures, inbox/
  scripts/             temporary bpy payloads
  renders/             verification screenshots, previews
  output/              final images / animations
  docs/                shot plans and notes
```

## Development Tasks

- **Run diagnostics**: `python cli.py --doctor` — verifies LLM endpoint, Blender install, image service reachability
- **Run smoke tests**: `python tests/smoke.py` — offline checks (project, gates, agent loop, mock 3D APIs); add `--blender` for actual Blender builds, `--net` for network calls
- **Add a new project**: `python cli.py --new <name>` or interactively via `python cli.py`
- **Generate concept images**: The `generate_references` tool creates concept art; always show to user and wait for approval before building
- **Search free assets**: `search_assets` tool queries Poly Haven (CC0 models/HDRI/textures) and ambientCG (CC0 materials)
- **Generate AI 3D mesh**: `generate_mesh` tool uses Tripo or Meshy (requires API keys in `.env`); falls back to manual inbox workflow without keys
- **Analyze a model**: `analyze_model` tool inspects a GLB in throwaway Blender session (dimensions, scale, materials, non-manifold edges)
- **Render / screenshot**: `blender_run`, `blender_screenshot`, `blender_render` tools execute bpy code and render outputs

## Important Configuration

### `.env` file

Create `.env` next to `cli.py` (it is git-ignored; there is no template file, the README lists every setting) and configure:

- **LLM**: Pick one provider preset (Ollama local, Groq free tier, Gemini free tier, OpenRouter)
- **Blender**: Set `BLENDER_PATH` if not auto-detected
- **Images**: `IMAGE_PROVIDER=pollinations` (default, free) or `none` (upload references only)
- **AI 3D**: `AI3D_PROVIDER=auto` (default); set `TRIPO_API_KEY` or `MESHY_API_KEY` for image-to-3D
- **Rendering**: `PREVIEW_ENGINE=auto`, `FINAL_ENGINE=CYCLES`

### Settings read from `.env` (defaults are in `directorc/config.py`)

- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_MAX_TOKENS`, `LLM_MAX_INPUT_TOKENS`, `VISION`
- `BLENDER_PATH` (optional)
- `IMAGE_PROVIDER`, `POLLINATIONS_MODEL`, `POLLINATIONS_API_KEY` (optional)
- `AI3D_PROVIDER`, `TRIPO_API_KEY`, `TRIPO_MODEL`, `MESHY_API_KEY`, `AI3D_TIMEOUT`
- `PREVIEW_ENGINE`, `FINAL_ENGINE`

## Notes

- The agent requires vision-capable models (Qwen 2.5, Llama 3.x/4, Gemini, GPT-4o class). Small text-only models will not work.
- Pollinations free tier: 1 image every 45s per IP; the app paces and retries automatically.
- AI image-to-3D (Tripo/Meshy) requires paid credits; without keys the agent guides through the manual inbox flow.
- Blender 3.6+ required; tested on 5.3. Portable copy exists in `tools/` and is auto-detected.
- FFmpeg on PATH is needed to encode MP4 animations (Blender 5.x no longer exposes FFmpeg output).
- All free assets (Poly Haven, ambientCG) are CC0 — license is recorded per asset in `manifest.json`.