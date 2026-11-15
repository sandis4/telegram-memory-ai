# telegram-memory-ai

A Telegram bot powered by any OpenAI-compatible API (OpenRouter, DeepSeek, Groq, …)
or the native Anthropic Claude API, with persistent user notes as long-term memory,
rich streaming replies and web search.

The bot targets **Bot API 10.3** through **aiogram 3.31**: replies are streamed as
Rich Message drafts, finalized as `sendRichMessage`, and can be stopped mid-generation.

---

## Features

- Streaming replies as Rich Messages (`sendRichMessageDraft` → `sendRichMessage`)
- **Stop button** while the model is generating (Bot API 10.3 `can_stop` +
  `stopped_message_generation` update); the partial answer is kept and saved to history
- **Ephemeral answers in groups** (Bot API 10.2+): only the requester and the bot see
  the answer, including streamed edits; can be disabled with `EPHEMERAL_GROUPS=0`
- **Source buttons** under web-search answers (Bot API 10.3 rich buttons)
- Web search and fresh news via `ddgs`, with the search query rewritten from the dialog
- Persistent memory files (`memory/*.md|txt`) attached to a message with `[file-N]`
- **OpenAI-compatible and native Claude providers** (`AI_PROVIDER=openai|anthropic`)
- Model fallback chain, shared-pool 429 handling with `Retry-After`
- Fallbacks: rich → plain draft → regular message edits; rich → plain text
- Pygame editor for memory files (`memory.py`)

---

## Requirements

- Python 3.11+
- An OpenAI-compatible endpoint + key, or an Anthropic API key
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Installation

```bash
git clone https://github.com/sandis4/telegram-memory-ai.git
cd telegram-memory-ai
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configuration (.env)

```dotenv
# openai | anthropic | auto
AI_PROVIDER=auto

# OpenAI-compatible API base URL
AI_API_URL=https://openrouter.ai/api/v1

AI_API_KEY=sk-REPLACE_WITH_YOUR_KEY
AI_MODEL=nvidia/nemotron-3-super-120b-a12b:free

# Optional: low / medium / high (empty — do not send)
AI_REASONING=low

# Max output tokens (required for Claude, optional for OpenAI)
AI_MAX_TOKENS=8192

# Optional: comma-separated fallback models
AI_MODEL_FALLBACK=z-ai/glm-5.2:free,nvidia/nemotron-3-super-120b-a12b:free

TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# Ephemeral answers in groups (1 = only requester sees them, 0 = public)
EPHEMERAL_GROUPS=1
```

Never commit real keys: `.env` is gitignored.

### Providers

**OpenAI-compatible (default).** Works with OpenRouter, DeepSeek, Groq and other
`/v1/chat/completions` services. Claude models served by OpenRouter also work here.

**Native Claude (Anthropic Messages API).** Use a direct Anthropic key:

```dotenv
AI_PROVIDER=anthropic
AI_API_URL=https://api.anthropic.com
AI_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-5-5
AI_MODEL_FALLBACK=claude-haiku-5-5
AI_REASONING=adaptive
AI_MAX_TOKENS=8192
```

- `AI_PROVIDER=auto` (or empty) detects Anthropic from the URL or an `sk-ant-` key.
- `AI_MODEL_FALLBACK` must contain Claude model IDs when using the native provider.
- Aliases are accepted: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_API_KEY`,
  `CLAUDE_API_URL`, `CLAUDE_MODEL`, `CLAUDE_MODEL_FALLBACK`. When
  `AI_PROVIDER=anthropic`, provider-specific aliases take precedence over generic
  `AI_*` variables.
- For proxies that use `Authorization: Bearer` instead of `x-api-key`, set
  `ANTHROPIC_AUTH_TOKEN` (or `CLAUDE_AUTH_TOKEN`); it replaces the API key.
- `AI_REASONING` for Claude: `low` / `medium` / `high` (thinking budgets on 4.x;
  Claude 5.x accepts only `adaptive`, so these map to `adaptive`), `adaptive`, or
  `disabled`. Empty — the `thinking` parameter is not sent.
- Thinking deltas are streamed into the draft; the finalized answer is text.
- History stores assistant text only. For plain chat Anthropic allows omitting previous
  thinking blocks; thinking continuity across turns is not preserved.
- A custom Anthropic-compatible endpoint works too: set `AI_PROVIDER=anthropic` and
  `AI_API_URL` to its base URL (without `/v1`).

---

## Usage

```bash
python bot.py       # run the bot
python memory.py    # open the pygame memory editor
```

Commands:

- `/start` — help
- `/files` — list memory files with `[file-N]` tags
- `/web query` — web search
- `/news topic` — fresh news
- `/clear` — forget the dialog history
- `/delete` — delete the bot's messages in the chat (and the user's ephemeral replies)

Attach memory to a message with a tag: `[file-1] Summarize this file`.
Web search also works inline: `[web] latest Telegram Bot API changes`.

---

## How streaming works

1. The provider layer (`OpenAI-compatible` or `Anthropic Messages`) emits the same
   `(text, thinking)` deltas; reasoning/thinking appears in the draft.
2. In private chats the bot streams `sendRichMessageDraft` (reasoning models show a
   `<tg-thinking>` block) and finalizes with `sendRichMessage`.
3. If rich drafts are unavailable it falls back to `sendMessageDraft`, then to editing a
   regular message.
4. In groups (with `EPHEMERAL_GROUPS=1`) the answer is sent as an ephemeral message and
   edited in place; if that fails, the bot answers publicly.
5. Pressing **Stop** sends a `stopped_message_generation` update; the stream stops and the
   partial answer is persisted with a note.

Bot API 10.4 features (HTML widgets, TON wallets, gifts, chat share themes) are not used:
the bot has no feature that needs them. aiogram 3.31 fully covers Bot API 10.3.

---

## Security & Privacy

- Do not commit API keys or bot tokens.
- Memory files are stored as plain text in `memory/`. Consider encryption and retention
  policies if you store sensitive data.
- Ephemeral group replies are visible only to the requester and the bot, but they are not
  a substitute for end-to-end encryption.

---

## License

MIT — see [LICENSE](LICENSE).
