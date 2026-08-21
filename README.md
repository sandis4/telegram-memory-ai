# telegram-memory-ai

A small experiment: a Telegram bot powered by an OpenAI-compatible model with simple conversational memory.

This repository contains a Python-based Telegram bot that uses a configurable OpenAI-compatible API endpoint (for example OpenRouter, DeepSeek, or other providers) to generate responses and keep short-term memory of conversations.

---

## Features

- Connects a Telegram bot to an OpenAI-compatible API
- Configurable model and fallback models
- Configurable reasoning settings (latency vs quality)
- Lightweight memory to provide context-aware replies (project experiment)

---

## Requirements

- Python 3.10+
- pip
- A running OpenAI-compatible API endpoint and API key
- A Telegram bot token (get one from @BotFather)

---

## Installation

1. Clone the repository:

   git clone https://github.com/sandis4/telegram-memory-ai.git
   cd telegram-memory-ai

2. Create a virtual environment and install dependencies (if requirements.txt exists):

   python -m venv venv
   source venv/bin/activate  # or `venv\Scripts\activate` on Windows
   pip install -r requirements.txt

If there is no `requirements.txt`, install dependencies listed in the code (for example `python-telegram-bot`, `requests`, `httpx`, or an OpenAI-compatible client).

---

## Configuration (.env)

Create a `.env` file in the project root with the following variables (do NOT commit real secrets):

```dotenv
# OpenAI-compatible API base URL (examples: OpenRouter, DeepSeek, Groq)
AI_API_URL=https://openrouter.ai/api/v1

# API key for the model provider
AI_API_KEY=sk-REPLACE_WITH_YOUR_KEY

# Model identifier
AI_MODEL=nvidia/nemotron-3-super-120b-a12b:free

# Optional: reasoning/latency tuning (low / medium / high)
AI_REASONING=low

# Optional: comma-separated fallback models
AI_MODEL_FALLBACK=z-ai/glm-5.2:free,nvidia/nemotron-3-super-120b-a12b:free

# Telegram bot token from @BotFather
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

Notes:
- Keep your API keys and bot tokens private. Add `.env` to `.gitignore` (already present in this repo) so secrets are not committed.

---

## Usage

1. Ensure the `.env` file is populated with correct variables.
2. Start the bot:

   python main.py

Replace `main.py` with the actual entrypoint filename if different.

Once the bot runs, open Telegram and talk to your bot. The bot will forward messages to the configured model and use a short-term memory to keep context (implementation details depend on code in this repo).

---

## Development & Contributing

- Feel free to open issues or pull requests.
- Suggested improvements:
  - Add tests and CI
  - Add clearer memory persistence (database or vector store)
  - Add rate-limit and error handling improvements

---

## Security & Privacy

- Do not store or expose API keys in public repositories.
- Be mindful of user data: if you store conversation history, consider encryption and retention policies.

---

## License

Specify a license for your project (e.g., MIT). If you have no preference, add an `LICENSE` file.

---

If you'd like, I can:
- Fill the README with specifics from the repository (entrypoint filename, dependencies) if you want — I can scan the repo and update accordingly.
- Create a minimal `requirements.txt` or add an example `main.py` run command based on the code.
