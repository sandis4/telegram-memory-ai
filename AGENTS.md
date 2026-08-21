# AGENTS.md

Project knowledge for AI agents working in this repo. Respond to the user in Russian.

## What this is

Telegram AI bot with persistent user memory,
plus a pygame GUI to manage that memory. Python 3.11+.

- `bot.py` — aiogram 3.30 bot. Talks to an OpenAI-compatible API (OpenRouter).
- `memory.py` — pygame editor for `memory/*.md|*.txt` files.
- `.env` — config template with `paste_your_...` placeholders. Fill in your own keys.

## Run / test

```powershell
python bot.py        # run the bot
python memory.py     # run the memory editor
python -m py_compile bot.py   # syntax check
```

Smoke tests: write temp scripts OUTSIDE the repo (e.g. system temp dir) and run
with `python -X utf8`. Load bot.py without running main():

```python
import importlib.util
spec = importlib.util.spec_from_file_location("b", "bot.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
```

Pygame tests need `SDL_VIDEODRIVER=dummy`.

## Conventions

- Memory file tags: `[file-N]`, N = 1-based index of `memory/*.md|*.txt` sorted by
  `p.name.lower()`. Both bot.py and memory.py must use identical ordering.
- Web search triggers: `[web]`, `[поиск]`, `[интернет]` tag in a normal message,
  `/web <query>` command, `/news <topic>` for news. Search lib: `ddgs`
  (metasearch: DuckDuckGo/Bing/Brave/Startpage). Before searching, the model
  rewrites the user's request into a self-contained query using dialog history
  (`make_search_query`, 1 attempt, falls back to the raw text). Top result's full
  text is extracted (`DDGS.extract`) and added as context.
- `strip_tags` removes only `[file-N]`; `strip_all_tags` also removes web tags.
- Handlers order matters: Command handlers are registered before the catch-all
  `F.text` handler. Current handlers: start, files, web, news, clear, delete, text.
- `/delete` wipes visible chat: tracked incoming ids (middleware `_track_incoming`)
  + brute-force id range scan in private chats (`DELETE_SCAN=400`). Telegram does
  not let bots delete messages older than 48h — unfixable.
- Draft streaming shows only the last 2 lines of reasoning inside `<tg-thinking>`.

## Non-obvious facts

- Bot API 10.1/10.2 Rich Messages (`sendRichMessage`, `sendRichMessageDraft`,
  `rich_message={"markdown": ...}`): aiogram 3.30 does NOT support them — bot.py
  calls them directly via module-level `tg_http` (httpx.AsyncClient). Drafts need
  the same non-zero `draft_id` to animate; `<tg-thinking>` blocks render in drafts.
- Reasoning models stream `reasoning`/`reasoning_content` deltas long before any
  content — surface them in drafts or the user sees nothing until finalize.
- Free OpenRouter models hit shared-pool 429s: client uses `max_retries=0`,
  own retry loop honoring Retry-After (`retry_delay()`), model fallback chain
  from `AI_MODEL_FALLBACK`.
- `.env` keys: AI_API_URL, AI_API_KEY, AI_MODEL, AI_REASONING (low/medium/high),
  AI_MODEL_FALLBACK, TELEGRAM_BOT_TOKEN. Placeholder guard checks
  `startswith(("сюда", "paste_your"))`.

## Key constants (bot.py)

TG_LIMIT=4096, CHUNK_SAFE=3900, RICH_LIMIT=30000, EDIT_INTERVAL=1.5,
DRAFT_INTERVAL=1.0, FIRST_EDIT_CHARS=50, MAX_HISTORY=10, MAX_MSG_CHARS=1500,
MAX_HISTORY_CHARS=6000, MAX_MEMORY_CHARS=100000, WEB_RESULTS=6,
WEB_SNIPPET_CHARS=500, WEB_EXTRACT_CHARS=6000, DDG_TIMEOUT=15,
CHAT_MSG_LIMIT=800, DELETE_SCAN=400.

## Hygiene

- `memory/` holds the user's real notes — never leave test files there.
- Never commit secrets; `.env*` and `__pycache__/` are gitignored.
