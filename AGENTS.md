# AGENTS.md

Project knowledge for AI agents working in this repo. Respond to the user in Russian.

## What this is

Telegram AI bot with persistent user memory, plus a pygame GUI to manage that memory.
Python 3.11+.

- `bot.py` — aiogram 3.31 bot targeting Bot API 10.3. Provider layer supports
  OpenAI-compatible APIs (`openai` 3.51) and native Anthropic Claude (`anthropic` 2.8),
  both HTTPX2-based.
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
  `F.text` handler. Current handlers: start, files, web, news, clear, delete, text,
  plus `stopped_message_generation`.
- `/delete` wipes visible chat: private — tracked incoming ids (middleware
  `_track_incoming`) + brute-force id range scan (`DELETE_SCAN=400`); groups — only
  the bot's own messages (`own_msgs`) plus the user's ephemeral messages. Telegram
  does not let bots delete messages older than 48h — unfixable.
- Draft streaming shows only the last 2 lines of reasoning inside `<tg-thinking>`.

## Non-obvious facts

- Bot API 10.1–10.3 Rich Messages are supported natively by aiogram 3.31:
  `Bot.send_rich_message`, `Bot.send_rich_message_draft`, `Bot.edit_message_text(
  rich_message=...)`, `InputRichMessage`, `InputRichBlock*`, `RichMessageButton`.
  Do NOT add raw HTTP calls for them.
- Bot API 10.3 `sendRichMessageDraft`/`sendMessageDraft` accept `can_stop=True`. The
  stop update arrives as `Update.stopped_message_generation`; aiogram exposes it as
  `@dp.stopped_message_generation()`. Active drafts are tracked in `active_drafts`
  (`draft_id → StreamHandle`) so the handler can set `StreamHandle.stop`.
- Streaming fallback chain (private): `send_rich_message_draft` → `send_message_draft`
  → regular message edits. In groups: ephemeral message + `edit_ephemeral_message_text`
  → public message edits. `EPHEMERAL_GROUPS` (default 1) toggles ephemeral group replies.
- Ephemeral messages: send via `send_message(..., ephemeral_message_parameters=
  EphemeralMessageParameters(receiver_user_id=...))`. They have `ephemeral_message_id`
  and `receiver_user`, NOT a normal deletable `message_id`; edit with
  `edit_ephemeral_message_text`/`edit_ephemeral_message_caption`, delete with
  `delete_ephemeral_message`. `Message.delete()` does not work for them.
- `sendRichMessageDraft` and `sendMessageDraft` work only in private chats. Group
  streaming must use ephemeral edits or public message edits.
- Rich message source buttons are appended as raw `<tg-button-row><tg-button ...>`
  markdown to the *rich* body only. The plain-text fallback deliberately excludes the
  suffix so users never see raw HTML.
- AI streaming is handled by `send_streaming`, which returns `(text, error, stopped)`.
  `generate_and_reply` owns history updates and the non-streaming `ask_ai` fallback;
  it always delivers a fallback answer (old bug: answer was lost).
- Provider selection: `AI_PROVIDER=openai|anthropic|auto` (empty = auto). Auto detects
  Anthropic from `AI_API_URL` containing "anthropic" or an `sk-ant-` key. With explicit
  `AI_PROVIDER=anthropic`, aliases `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`CLAUDE_*`
  take precedence over generic `AI_*`; the URL has trailing `/v1`, `/messages` or
  `/v1/messages` stripped because the SDK appends `/v1/messages`.
- Anthropic path: `anthropic_payload()` converts OpenAI-style system/messages to the
  Messages API (system hoisted, same-role merged, leading assistant dropped), always
  sends `max_tokens=AI_MAX_TOKENS`. Thinking is chosen per model by
  `anthropic_thinking_for_model()`: low/medium/high → `enabled` + budget (clamped below
  `max_tokens`) on 4.x, but **Claude 5.x accepts only `adaptive`** (a 400 otherwise), so
  5.x models map low/medium/high to adaptive. `adaptive`/`disabled` pass through; empty →
  omit. `anthropic_create()` retries without thinking if a 400 mentions thinking.
  Do not set `AI_REASONING=adaptive` for OpenAI.
- `open_stream()` + `iter_stream_deltas()` unify both providers into `(text, thinking)`
  deltas. Anthropic streaming uses `content_block_delta` with `text_delta` /
  `thinking_delta`; non-streaming answers come from `Message.content` blocks.
- Anthropic thinking blocks are NOT stored in history (text-only history), so thinking
  continuity is lost across turns. This is allowed for plain multi-turn chat (no tools);
  do not enable tools without preserving thinking blocks.
- Anthropic client init can fail on a malformed `NO_PROXY` (e.g. `[::1]`) inside HTTPX2.
  `_build_anthropic_client()` retries with `httpx2.AsyncClient(trust_env=False)`.
- Both `openai` 3.x and `anthropic` 2.x use HTTPX2 and do not depend on `httpx`. Use
  `openai.Timeout`/`anthropic.Timeout` and do not import `httpx`.
- Reasoning models stream `reasoning`/`reasoning_content` deltas long before any
  content — surface them in drafts or the user sees nothing until finalize.
- Free OpenRouter models hit shared-pool 429s: client uses `max_retries=0`,
  own retry loop honoring Retry-After (`retry_delay()`), model fallback chain
  from `AI_MODEL_FALLBACK`.
- `.env` keys: AI_PROVIDER, AI_API_URL, AI_API_KEY, AI_MODEL, AI_REASONING
  (low/medium/high; Claude also adaptive/disabled), AI_MAX_TOKENS, AI_MODEL_FALLBACK,
  TELEGRAM_BOT_TOKEN, EPHEMERAL_GROUPS. Anthropic also accepts
  `ANTHROPIC_AUTH_TOKEN`/`CLAUDE_AUTH_TOKEN` (Bearer auth for proxies; replaces the API
  key and satisfies the "key is set" check). Placeholder guard checks
  `startswith(("сюда", "paste_your"))`.
- If `AI_PROVIDER=anthropic` but `AI_API_URL` still points to a known OpenAI-compatible
  host (OpenRouter/OpenAI/DeepSeek/Groq), import raises a clear RuntimeError.

## Key constants (bot.py)

TG_LIMIT=4096, CHUNK_SAFE=3900, RICH_LIMIT=30000, EDIT_INTERVAL=1.5,
DRAFT_INTERVAL=1.0, FIRST_EDIT_CHARS=50, MAX_HISTORY=10, MAX_MSG_CHARS=1500,
MAX_HISTORY_CHARS=6000, MAX_MEMORY_CHARS=100000, WEB_RESULTS=6,
WEB_SNIPPET_CHARS=500, WEB_EXTRACT_CHARS=6000, DDG_TIMEOUT=15,
CHAT_MSG_LIMIT=800, DELETE_SCAN=400, AI_MAX_TOKENS=8192 (default, env).

## Hygiene

- `memory/` holds the user's real notes — never leave test files there.
- Never commit secrets; `.env*` and `__pycache__/` are gitignored.
