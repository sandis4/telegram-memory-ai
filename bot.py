"""Telegram AI bot with persistent memory. Python 3.11+, aiogram 3.31, Bot API 10.3."""

import asyncio
import html
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    EphemeralMessageParameters,
    InputRichMessage,
    Message,
    MessageGenerationStopped,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI, Timeout

try:
    from anthropic import AsyncAnthropic, Timeout as AnthropicTimeout
except ImportError:  # пакет не установлен — режим Claude недоступен
    AsyncAnthropic = None
    AnthropicTimeout = None

load_dotenv()

def _clean_secret(value: str) -> str:
    return "" if value.startswith(("сюда", "paste_your")) else value


AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
_ai_url = os.getenv("AI_API_URL", "").strip()
_ai_key = _clean_secret(os.getenv("AI_API_KEY", "").strip())
_ai_model = os.getenv("AI_MODEL", "").strip()
_ai_fallback = os.getenv("AI_MODEL_FALLBACK", "").strip()
_alias_url = (
    os.getenv("ANTHROPIC_BASE_URL", "").strip()
    or os.getenv("CLAUDE_API_URL", "").strip()
)
_alias_key = _clean_secret(
    os.getenv("ANTHROPIC_API_KEY", "").strip()
    or os.getenv("CLAUDE_API_KEY", "").strip()
)
_alias_model = os.getenv("CLAUDE_MODEL", "").strip()
_alias_fallback = os.getenv("CLAUDE_MODEL_FALLBACK", "").strip()
_OPENAI_HOSTS = ("openrouter.ai", "api.openai.com", "api.deepseek.com", "api.groq.com")

if AI_PROVIDER in ("", "auto"):
    AI_PROVIDER = "anthropic" if (
        "anthropic" in _ai_url.lower() or _ai_key.startswith("sk-ant-")
    ) else "openai"
elif AI_PROVIDER in ("claude", "anthropic"):
    AI_PROVIDER = "anthropic"
elif AI_PROVIDER in ("openai", "openrouter", "compatible"):
    AI_PROVIDER = "openai"
else:
    raise RuntimeError(f"Unknown AI_PROVIDER={AI_PROVIDER!r}; use openai, anthropic or auto")

AI_AUTH_TOKEN = ""
if AI_PROVIDER == "anthropic":
    # Provider-specific aliases win when the provider is chosen explicitly
    AI_API_URL = (_alias_url or _ai_url or "https://api.anthropic.com").rstrip("/")
    for suffix in ("/v1/messages", "/messages", "/v1"):  # SDK сам добавляет /v1/messages
        if AI_API_URL.endswith(suffix):
            AI_API_URL = AI_API_URL[: -len(suffix)].rstrip("/")
            break
    AI_API_KEY = _alias_key or _ai_key
    AI_AUTH_TOKEN = _clean_secret(
        os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
        or os.getenv("CLAUDE_AUTH_TOKEN", "").strip()
    )
    AI_MODEL = _alias_model or _ai_model or "claude-sonnet-5-5"
    _fallback_raw = _alias_fallback or _ai_fallback
else:
    AI_API_URL = _ai_url or "https://openrouter.ai/api/v1"
    AI_API_KEY = _ai_key
    AI_MODEL = _ai_model or "openai/gpt-4o-mini"
    _fallback_raw = _ai_fallback

# Запасные модели через запятую: используются по очереди при rate-limit основной
AI_MODEL_FALLBACK = [m.strip() for m in _fallback_raw.split(",") if m.strip()]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Ускоряет «думающие» модели: low/medium/high (OpenAI) или low/medium/high/adaptive/disabled (Claude).
AI_REASONING = os.getenv("AI_REASONING", "")
try:
    AI_MAX_TOKENS = max(1, int(os.getenv("AI_MAX_TOKENS", "8192")))
except ValueError:
    AI_MAX_TOKENS = 8192
# Эфемерные ответы в группах: сообщение видит только автор запроса и бот (Bot API 10.2+)
EPHEMERAL_GROUPS = os.getenv("EPHEMERAL_GROUPS", "1").strip().lower() not in ("0", "false", "no", "off")

MEMORY_DIR = Path(__file__).parent / "memory"
MAX_HISTORY = 10             # пар сообщений в истории (было 20 — тормозило запросы)
MAX_MSG_CHARS = 1500         # обрезка старых сообщений в истории
MAX_HISTORY_CHARS = 6000     # суммарный лимит истории
MAX_MEMORY_CHARS = 100_000
SEND_ALL_WITHOUT_TAGS = False  # True — без [file-N] отправлять всю память

TG_LIMIT = 4096
CHUNK_SAFE = 3900
RICH_LIMIT = 30000   # безопасный лимит rich-сообщения (максимум 32768)
EDIT_INTERVAL = 1.5   # секунды между правками обычных сообщений (антифлуд Telegram)
DRAFT_INTERVAL = 1.0  # секунды между обновлениями черновика
FIRST_EDIT_CHARS = 50  # первый показ текста почти сразу
FILE_TAG_RE = re.compile(r"\[file-(\d+)\]", re.IGNORECASE)
WEB_TAG_RE = re.compile(r"\[(?:web|поиск|интернет)\]", re.IGNORECASE)
GROUP_CHAT_TYPES = ("group", "supergroup")

# ---------------- поиск в интернете (ddgs) ----------------
WEB_RESULTS = 6           # сколько результатов отдавать нейросети
WEB_SNIPPET_CHARS = 500   # обрезка сниппета
WEB_EXTRACT_CHARS = 6000  # сколько текста статьи забирать целиком
DDG_TIMEOUT = 15

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")

if AI_PROVIDER == "anthropic":
    if any(host in AI_API_URL for host in _OPENAI_HOSTS):
        raise RuntimeError(
            "AI_PROVIDER=anthropic, но AI_API_URL указывает на OpenAI-совместимый сервис. "
            "Укажи AI_API_URL=https://api.anthropic.com (или свой Claude endpoint) "
            "либо переключи AI_PROVIDER=openai."
        )
    if AI_MODEL and not AI_MODEL.lower().startswith("claude"):
        logger.warning("AI_PROVIDER=anthropic, но AI_MODEL=%r не похож на Claude-модель", AI_MODEL)

client = None
anthropic_client = None
EXTRA_BODY: dict = {}


def _build_anthropic_client():
    if AsyncAnthropic is None or AnthropicTimeout is None:
        raise RuntimeError(
            "AI_PROVIDER=anthropic, но пакет anthropic не установлен: pip install anthropic"
        )
    auth = {"auth_token": AI_AUTH_TOKEN} if AI_AUTH_TOKEN else {"api_key": AI_API_KEY}
    kwargs = {
        **auth,
        "base_url": AI_API_URL,
        "timeout": AnthropicTimeout(90.0, connect=10.0),
        "max_retries": 0,  # ретраи делаем сами
    }
    try:
        return AsyncAnthropic(**kwargs)
    except Exception as e:
        # httpx2 падает на некоторых значениях NO_PROXY (например, "[::1]")
        logger.warning("Не удалось создать клиент Anthropic (%s), повтор без env-прокси", e)
        import httpx2

        return AsyncAnthropic(
            **kwargs,
            http_client=httpx2.AsyncClient(
                trust_env=False,
                timeout=httpx2.Timeout(90.0, connect=10.0),
            ),
        )


if AI_PROVIDER == "anthropic":
    anthropic_client = _build_anthropic_client()
else:
    client = AsyncOpenAI(
        base_url=AI_API_URL,
        api_key=AI_API_KEY or "sk-missing",  # пустой ключ не даёт создать клиент
        timeout=Timeout(90.0, connect=10.0),  # не висеть минутами при сбое
        max_retries=0,  # ретраи делаем сами, с учётом Retry-After
    )
    if AI_REASONING.lower() in ("low", "medium", "high"):
        EXTRA_BODY = {"reasoning": {"effort": AI_REASONING.lower()}}
    elif AI_REASONING:
        logger.warning(
            "AI_REASONING=%r не поддерживается OpenAI-совместимым провайдером",
            AI_REASONING,
        )

if not (AI_API_KEY or AI_AUTH_TOKEN):
    logger.warning("AI_API_KEY не заполнен — бот попросит ключ при первом сообщении")

dp = Dispatcher()

histories: dict[int, list[dict]] = {}

# трекинг сообщений чата для /delete
CHAT_MSG_LIMIT = 800
DELETE_SCAN = 400  # сколько последних ID сканировать в личном чате
chat_msgs: dict[int, deque[int]] = {}
own_msgs: dict[int, deque[int]] = {}
eph_msgs: dict[tuple[int, int], deque[int]] = {}


def track(chat_id: int, *message_ids: int) -> None:
    dq = chat_msgs.setdefault(chat_id, deque(maxlen=CHAT_MSG_LIMIT))
    for mid in message_ids:
        if mid:
            dq.append(mid)


def track_own(chat_id: int, *message_ids: int) -> None:
    dq = own_msgs.setdefault(chat_id, deque(maxlen=CHAT_MSG_LIMIT))
    for mid in message_ids:
        if mid:
            dq.append(mid)


def track_sent(chat_id: int, message: Message) -> None:
    """Запоминает отправленное ботом сообщение (обычное или эфемерное)."""
    eid = getattr(message, "ephemeral_message_id", None)
    if eid:
        user = getattr(message, "receiver_user", None)
        if user:
            dq = eph_msgs.setdefault((chat_id, user.id), deque(maxlen=CHAT_MSG_LIMIT))
            dq.append(eid)
        return
    mid = getattr(message, "message_id", None)
    if mid:
        track(chat_id, mid)
        track_own(chat_id, mid)


@dp.message.middleware()
async def _track_incoming(handler, event: Message, data):
    # в группах не трогаем чужие сообщения: /delete удаляет только своё
    if event.chat.type == "private":
        track(event.chat.id, event.message_id)
    return await handler(event, data)


def ephemeral_params(message: Message) -> EphemeralMessageParameters | None:
    if (
        EPHEMERAL_GROUPS
        and message.chat.type in GROUP_CHAT_TYPES
        and message.from_user is not None
    ):
        return EphemeralMessageParameters(receiver_user_id=message.from_user.id)
    return None


async def reply(message: Message, text: str, **kwargs) -> Message:
    """Ответ в текущем треде; в группах — эфемерный (если включено)."""
    if message.message_thread_id:
        kwargs.setdefault("message_thread_id", message.message_thread_id)
    eph = ephemeral_params(message)
    if eph:
        kwargs.setdefault("ephemeral_message_parameters", eph)
    try:
        sent = await message.answer(text, **kwargs)
    except TelegramBadRequest as e:
        if not eph:
            raise
        logger.warning("Эфемерный ответ не удался (%s), отправляю публично", e)
        kwargs.pop("ephemeral_message_parameters", None)
        sent = await message.answer(text, **kwargs)
    track_sent(message.chat.id, sent)
    return sent


async def delete_message_quiet(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def delete_ephemeral_quiet(bot: Bot, chat_id: int, user_id: int, eph_id: int) -> bool:
    try:
        await bot.delete_ephemeral_message(chat_id, user_id, eph_id)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.delete_ephemeral_message(chat_id, user_id, eph_id)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def delete_msg(message: Message) -> None:
    eid = getattr(message, "ephemeral_message_id", None)
    user = getattr(message, "receiver_user", None)
    if eid and user:
        await delete_ephemeral_quiet(message.bot, message.chat.id, user.id, eid)
    else:
        await delete_message_quiet(message.bot, message.chat.id, message.message_id)


# ---------------- память ----------------

def numbered_files() -> list[Path]:
    if not MEMORY_DIR.exists():
        return []
    return sorted(
        (p for p in MEMORY_DIR.iterdir()
         if p.is_file() and p.suffix.lower() in (".md", ".txt")),
        key=lambda p: p.name.lower(),
    )


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def strip_tags(text: str) -> str:
    return FILE_TAG_RE.sub("", text).strip()


def strip_all_tags(text: str) -> str:
    return FILE_TAG_RE.sub("", WEB_TAG_RE.sub("", text)).strip()


def load_memory_by_tags(text: str) -> str:
    tags = sorted({int(m.group(1)) for m in FILE_TAG_RE.finditer(text)})
    files = numbered_files()
    if not tags:
        if not SEND_ALL_WITHOUT_TAGS:
            return ""
        chosen = files
    else:
        chosen = [files[n - 1] for n in tags if 1 <= n <= len(files)]
    parts: list[str] = []
    total = 0
    for path in chosen:
        body = read_file(path)
        if total + len(body) > MAX_MEMORY_CHARS:
            body = body[: max(0, MAX_MEMORY_CHARS - total)]
        parts.append(f"### Файл памяти: {path.name}\n{body}")
        total += len(body)
        if total >= MAX_MEMORY_CHARS:
            break
    return "\n\n".join(parts)


def build_messages(user_id: int, memory: str = "", extra: str = "") -> list[dict]:
    system = "Ты — полезный ИИ-ассистент в Telegram."
    if memory:
        system += (
            "\nНиже — память пользователя (его заметки). "
            "Опирайся на неё при ответах.\n\n" + memory
        )
    if extra:
        system += "\n" + extra
    messages = [{"role": "system", "content": system}]
    messages.extend(histories.get(user_id, []))
    return messages


# ---------------- поиск в интернете ----------------

def _ddg_text(query: str) -> list[dict]:
    with DDGS(timeout=DDG_TIMEOUT) as d:
        return d.text(query, max_results=WEB_RESULTS)


def _ddg_news(query: str) -> list[dict]:
    with DDGS(timeout=DDG_TIMEOUT) as d:
        return d.news(query, max_results=WEB_RESULTS)


def _ddg_extract(url: str) -> dict:
    with DDGS(timeout=DDG_TIMEOUT) as d:
        return d.extract(url)


async def web_search(query: str, news: bool = False) -> tuple[list[dict], str]:
    if DDGS is None:
        return [], "библиотека ddgs не установлена (pip install ddgs)"
    try:
        fn = _ddg_news if news else _ddg_text
        return await asyncio.to_thread(fn, query), ""
    except Exception as e:
        logger.warning("Поиск не удался: %s", e)
        return [], f"{type(e).__name__}: {e}"


def format_search(results: list[dict], news: bool = False) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = r.get("href") or r.get("url") or ""
        body = (r.get("body") or "").strip()[:WEB_SNIPPET_CHARS]
        head = f"[{i}] {title}\n{url}"
        if news:
            meta = " | ".join(str(x) for x in (r.get("source"), r.get("date")) if x)
            if meta:
                head += f"\n{meta}"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def sources_buttons(results: list[dict]) -> str:
    """Ряд кнопок-ссылок на источники (Rich Messages, Bot API 10.3)."""
    seen: set[str] = set()
    buttons: list[str] = []
    for r in results:
        url = (r.get("href") or r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        host = urlparse(url).netloc.removeprefix("www.")
        label = (host or "ссылка")[:32]
        buttons.append(
            f'<tg-button type="url" url="{html.escape(url, quote=True)}">'
            f"{html.escape(label, quote=False)}</tg-button>"
        )
        if len(buttons) >= 6:
            break
    if not buttons:
        return ""
    return '\n\n<tg-button-row align="left">' + "".join(buttons) + "</tg-button-row>"


QUERY_SYSTEM = (
    "Ты помогаешь делать веб-поиск. На основе истории диалога преврати запрос "
    "пользователя в короткий самодостаточный поисковый запрос: добавь пропущенный "
    "контекст (о чём шла речь), убери лишние слова. "
    "Если запрос уже самодостаточен — верни его без изменений. "
    "Ответь только самим запросом, одной строкой, без кавычек и пояснений."
)


async def make_search_query(user_id: int, query: str) -> str:
    """Формулирует поисковый запрос с учётом истории диалога."""
    hist = histories.get(user_id, [])[:-1]  # текущий запрос уже в истории
    if not hist:
        return query
    ctx = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in hist[-6:])
    messages = [
        {"role": "system", "content": QUERY_SYSTEM},
        {"role": "user", "content": f"История диалога:\n{ctx}\n\nЗапрос: {query}"},
    ]
    try:
        answer, _ = await ask_ai(messages, attempts=1)
    except Exception:
        return query
    if not answer:
        return query
    q = answer.strip().splitlines()[0].strip(' "\'«»')[:200].strip()
    return q or query


async def answer_with_web(message: Message, query: str, news: bool = False) -> None:
    """Ищет в интернете и отвечает нейросетью с опорой на найденное."""
    query = strip_all_tags(query)
    user_id = message.from_user.id
    history = histories.setdefault(user_id, [])
    prefix = "Новости: " if news else "Поиск: "
    history.append({"role": "user", "content": prefix + query})

    status = await reply(message, "Ищу новости..." if news else "Ищу в интернете...")
    search_q = await make_search_query(user_id, query)
    results, err = await web_search(search_q, news)

    extra = ""
    if results:
        kind = "свежие новостные статьи" if news else "результаты веб-поиска"
        extra = (
            f"\nНиже — {kind} по запросу «{search_q}» "
            f"(пользователь просил: «{query}»). "
            "Опирайся на них при ответе и указывай источники ссылками.\n\n"
            + format_search(results, news)
        )
        if not news and results[0].get("href"):
            try:  # полный текст первой статьи
                ext = await asyncio.to_thread(_ddg_extract, results[0]["href"])
                content = ext.get("content") or ""
                if isinstance(content, bytes):
                    content = content.decode("utf-8", "replace")
                content = content.strip()[:WEB_EXTRACT_CHARS]
                if len(content) > 200:
                    extra += (
                        f"\n\n### Полный текст первой ссылки ({results[0]['href']})\n"
                        + content
                    )
            except Exception:
                pass
    elif err:
        extra = (
            f"\nПоиск в интернете сейчас недоступен ({err}). "
            "Ответь из своих знаний и предупреди, что поиск не сработал."
        )

    await delete_msg(status)

    messages = build_messages(user_id, "", extra)
    await generate_and_reply(message, messages, history, rich_suffix=sources_buttons(results))


def trim_history(history: list[dict]) -> None:
    """Держит историю короткой: меньше токенов — быстрее ответ."""
    for m in history:
        if len(m["content"]) > MAX_MSG_CHARS:
            m["content"] = m["content"][:MAX_MSG_CHARS] + "…"
    total = sum(len(m["content"]) for m in history)
    while history and (len(history) > MAX_HISTORY * 2 or total > MAX_HISTORY_CHARS):
        total -= len(history[0]["content"])
        history.pop(0)
    # история не должна начинаться с ответа ассистента
    while history and history[0]["role"] == "assistant":
        history.pop(0)


# ---------------- markdown -> Telegram HTML ----------------

CODE_BLOCK_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
STASH_RE = re.compile(r"\x00(\d+)\x00")


def md_to_html(text: str) -> str:
    blocks: list[str] = []

    def stash(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00{len(blocks) - 1}\x00"

    text = CODE_BLOCK_RE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)

    def unstash(m: re.Match) -> str:
        return "<pre>" + html.escape(blocks[int(m.group(1))], quote=False) + "</pre>"

    return STASH_RE.sub(unstash, text)


async def edit_msg(msg: Message, text: str) -> bool:
    """Правит сообщение с HTML-разметкой; если разметка битая — обычным текстом."""
    try:
        await msg.edit_text(md_to_html(text), parse_mode="HTML")
        return True
    except Exception:
        try:
            await msg.edit_text(text[:TG_LIMIT])
            return True
        except Exception:
            return False


# ---------------- отрисовка ответа ----------------

def split_for_rich(text: str) -> list[str]:
    parts = []
    while len(text) > RICH_LIMIT:
        nl = text.rfind("\n", RICH_LIMIT - 2000, RICH_LIMIT)
        cut = nl + 1 if nl != -1 else RICH_LIMIT
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


async def send_plain(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    eph: EphemeralMessageParameters | None = None,
) -> None:
    """Отправка текста по кускам с обработкой флуд-лимитов."""
    for i in range(0, len(text), TG_LIMIT):
        part = text[i : i + TG_LIMIT]
        for attempt in range(2):
            try:
                m = await bot.send_message(
                    chat_id,
                    part,
                    message_thread_id=thread_id,
                    ephemeral_message_parameters=eph,
                )
                track_sent(chat_id, m)
                break
            except TelegramRetryAfter as e:
                if attempt == 0:
                    await asyncio.sleep(e.retry_after)
                else:
                    raise


async def send_rich_parts(
    bot: Bot,
    chat_id: int,
    parts: list[str],
    *,
    thread_id: int | None = None,
    eph: EphemeralMessageParameters | None = None,
) -> None:
    for part in parts:
        m = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(markdown=part),
            message_thread_id=thread_id,
            ephemeral_message_parameters=eph,
        )
        track_sent(chat_id, m)


async def deliver_final(
    bot: Bot,
    chat_id: int,
    plain_text: str,
    rich_text: str | None = None,
    *,
    thread_id: int | None = None,
    eph: EphemeralMessageParameters | None = None,
) -> bool:
    """Пытается отправить rich-сообщение, при сбое — обычный текст."""
    rich_text = plain_text if rich_text is None else rich_text
    if rich_text:
        try:
            await send_rich_parts(bot, chat_id, split_for_rich(rich_text), thread_id=thread_id, eph=eph)
            return True
        except Exception as e:
            logger.warning("Rich-сообщение не удалось (%s), отправка текстом", e)
    try:
        await send_plain(bot, chat_id, plain_text, thread_id=thread_id, eph=eph)
        return True
    except Exception as e:
        if eph:
            logger.warning("Эфемерная отправка не удалась (%s), отправляю публично", e)
            try:
                await send_plain(bot, chat_id, plain_text, thread_id=thread_id)
                return True
            except Exception as e2:
                logger.error("Отправка не удалась совсем: %s", e2)
        else:
            logger.error("Отправка не удалась совсем: %s", e)
    return False


async def deliver_answer(message: Message, text: str, rich_suffix: str = "") -> bool:
    return await deliver_final(
        message.bot,
        message.chat.id,
        text,
        text + rich_suffix,
        thread_id=message.message_thread_id,
        eph=ephemeral_params(message),
    )


# ---------------- запросы к нейросети ----------------

def all_models() -> list[str]:
    return [AI_MODEL] + AI_MODEL_FALLBACK


def retry_delay(e: Exception) -> float:
    """Достаёт Retry-After из ошибки (заголовок или тело), максимум 30 сек."""
    try:
        ra = getattr(getattr(e, "response", None), "headers", {}).get("retry-after")
        if ra:
            return min(float(ra), 30.0)
    except Exception:
        pass
    m = re.search(r"retry_after_seconds['\"]?\s*[:=]\s*(\d+)", str(e))
    if m:
        return min(float(m.group(1)), 30.0)
    return 3.0


def extract_answer(response) -> str | None:
    if not getattr(response, "choices", None):
        return None
    msg = response.choices[0].message
    content = (msg.content or "").strip()
    if content:
        return content
    return (
        getattr(msg, "reasoning_content", None)
        or getattr(msg, "reasoning", None)
        or ""
    ).strip() or None


def extract_anthropic_answer(response) -> str | None:
    texts: list[str] = []
    thoughts: list[str] = []
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            texts.append(getattr(block, "text", "") or "")
        elif kind == "thinking":
            thoughts.append(getattr(block, "thinking", "") or "")
    text = "\n".join(texts).strip()
    if text:
        return text
    return "\n".join(thoughts).strip() or None


def _is_claude5(model: str) -> bool:
    return bool(
        re.search(r"claude-(?:opus|sonnet|haiku|fable|mythos)-5(?:[.\-]|$)", model.lower())
    )


_thinking_warned: set[str] = set()


def _anthropic_thinking_param(model: str) -> dict | None:
    effort = AI_REASONING.strip().lower()
    if not effort:
        return None
    if effort in ("0", "off", "none", "disabled", "disable"):
        return {"type": "disabled"}
    if effort == "adaptive":
        return {"type": "adaptive"}
    budget = {"low": 1024, "medium": 4096, "high": 16384}.get(effort)
    if budget is None:
        logger.warning("Неизвестный AI_REASONING=%r для Claude, thinking не отправляется", AI_REASONING)
        return None
    if _is_claude5(model):
        # Claude 5.x поддерживает только adaptive и отвечает 400 на enabled+budget
        if model not in _thinking_warned:
            logger.info(
                "Claude 5.x поддерживает только adaptive thinking: AI_REASONING=%s → adaptive",
                effort,
            )
            _thinking_warned.add(model)
        return {"type": "adaptive"}
    budget = min(budget, AI_MAX_TOKENS - 1024)
    if budget < 1024:
        logger.warning("AI_MAX_TOKENS=%d слишком мал для thinking, thinking отключён", AI_MAX_TOKENS)
        return {"type": "disabled"}
    return {"type": "enabled", "budget_tokens": budget}


_anthropic_thinking_cache: dict[str, dict | None] = {}


def anthropic_thinking_for_model(model: str) -> dict | None:
    if model not in _anthropic_thinking_cache:
        _anthropic_thinking_cache[model] = _anthropic_thinking_param(model)
    return _anthropic_thinking_cache[model]


def anthropic_payload(
    messages: list[dict],
    model: str,
    *,
    stream: bool,
    allow_thinking: bool = True,
) -> dict:
    """OpenAI-style messages → Anthropic Messages API payload."""
    system_parts: list[str] = []
    converted: list[dict] = []
    for item in messages:
        role = item.get("role")
        content = item.get("content", "")
        if role == "system":
            system_parts.append(str(content))
            continue
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            converted.append({"role": role, "content": content})
        elif converted and converted[-1]["role"] == role and isinstance(converted[-1]["content"], str):
            converted[-1]["content"] += "\n\n" + str(content)
        else:
            converted.append({"role": role, "content": str(content)})
    while converted and converted[0]["role"] == "assistant":
        converted.pop(0)
    if not converted:
        converted = [{"role": "user", "content": "Продолжай."}]

    payload = {
        "model": model,
        "max_tokens": AI_MAX_TOKENS,
        "messages": converted,
        "stream": stream,
    }
    system = "\n\n".join(part for part in system_parts if part)
    if system:
        payload["system"] = system
    if allow_thinking:
        thinking = anthropic_thinking_for_model(model)
        if thinking is not None:
            payload["thinking"] = thinking
    return payload


def is_thinking_rejected(e: Exception) -> bool:
    text = str(e).lower()
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    return status == 400 and ("thinking" in text or "budget_tokens" in text)


async def anthropic_create(messages: list[dict], model: str, *, stream: bool):
    try:
        return await anthropic_client.messages.create(
            **anthropic_payload(messages, model, stream=stream)
        )
    except Exception as e:
        if not is_thinking_rejected(e):
            raise
        logger.warning("Claude отклонил thinking (%s), повтор без thinking", e)
        return await anthropic_client.messages.create(
            **anthropic_payload(messages, model, stream=stream, allow_thinking=False)
        )


async def open_stream(model: str, messages: list[dict]):
    if AI_PROVIDER == "anthropic":
        return await anthropic_create(messages, model, stream=True)
    return await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        extra_body=EXTRA_BODY,
    )


async def iter_stream_deltas(stream):
    """Единый поток (текст, размышления) для OpenAI-совместимых API и Anthropic."""
    if AI_PROVIDER == "anthropic":
        async for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            kind = getattr(delta, "type", None)
            if kind == "text_delta":
                yield getattr(delta, "text", None), None
            elif kind == "thinking_delta":
                yield None, getattr(delta, "thinking", None)
        return

    async for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if piece or reasoning:
            yield piece, reasoning


async def ask_ai(messages: list[dict], attempts: int = 4) -> tuple[str | None, str]:
    """Нестримовый запрос; модели перебираются по очереди при сбоях."""
    models = all_models()
    last_err = ""
    wait = 3.0
    for attempt in range(attempts):
        model = models[attempt % len(models)]
        try:
            if AI_PROVIDER == "anthropic":
                response = await anthropic_create(messages, model, stream=False)
                answer = extract_anthropic_answer(response)
            else:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    extra_body=EXTRA_BODY,
                )
                answer = extract_answer(response)
            if answer:
                return answer, ""
            last_err = "нейросеть вернула пустой ответ"
            logger.warning(
                "Пустой ответ (%s, попытка %d/%d): %s",
                model, attempt + 1, attempts, str(response.model_dump(exclude_none=True))[:500],
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            wait = retry_delay(e)
            logger.warning("Ошибка запроса (%s, попытка %d/%d): %s", model, attempt + 1, attempts, last_err)
        if attempt < attempts - 1:
            await asyncio.sleep(wait)
    return None, last_err


async def _typing_loop(bot: Bot, chat_id: int) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        await asyncio.sleep(4)


# ---------------- стриминг ответа ----------------

class StreamHandle:
    __slots__ = ("chat_id", "stop")

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.stop = asyncio.Event()


active_drafts: dict[int, StreamHandle] = {}


@dp.stopped_message_generation()
async def on_stopped_generation(event: MessageGenerationStopped) -> None:
    handle = active_drafts.get(event.draft_id)
    if handle and handle.chat_id == event.chat.id:
        handle.stop.set()
        logger.info("Пользователь остановил генерацию (draft %d)", event.draft_id)


async def _finish_ephemeral(
    bot: Bot,
    chat_id: int,
    eph: EphemeralMessageParameters,
    eph_id: int,
    thread_id: int | None,
    rich_body: str,
    plain_body: str,
) -> bool:
    parts = split_for_rich(rich_body)
    try:
        await bot.edit_ephemeral_message_text(
            chat_id,
            eph.receiver_user_id,
            eph_id,
            rich_message=InputRichMessage(markdown=parts[0]),
        )
        for part in parts[1:]:
            track_sent(
                chat_id,
                await bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=InputRichMessage(markdown=part),
                    message_thread_id=thread_id,
                    ephemeral_message_parameters=eph,
                ),
            )
        return True
    except Exception as e:
        logger.warning("Финальное эфемерное rich-сообщение не удалось (%s), текст", e)

    try:
        await bot.edit_ephemeral_message_text(
            chat_id, eph.receiver_user_id, eph_id, text=plain_body[:TG_LIMIT]
        )
        rest = plain_body[TG_LIMIT:]
        if rest:
            await send_plain(bot, chat_id, rest, thread_id=thread_id, eph=eph)
        return True
    except Exception as e:
        logger.warning("Эфемерная отправка не удалась (%s), публично", e)
        await delete_ephemeral_quiet(bot, chat_id, eph.receiver_user_id, eph_id)
        return False


async def send_streaming(
    message: Message,
    messages: list[dict],
    *,
    rich_suffix: str = "",
) -> tuple[str | None, str, bool]:
    """Стримит ответ и сохраняет его.
    private: rich-черновик → обычный черновик → правка сообщений.
    groups: эфемерные сообщения (если включено) → публичная правка сообщений.
    Возвращает (текст, ошибка, остановлено_пользователем)."""
    bot = message.bot
    chat_id = message.chat.id
    private = message.chat.type == "private"
    eph = None if private else ephemeral_params(message)
    thread_id = message.message_thread_id

    full = ""
    thinking = ""
    err = ""
    stopped = False
    draft_id = random.randint(1, 2**31 - 1) if private else 0
    handle = StreamHandle(chat_id) if private else None
    if handle:
        active_drafts[draft_id] = handle

    # rich_draft / plain_draft — только личные чаты; ephemeral / edits — группы
    mode = "rich_draft" if private else ("ephemeral" if eph else "edits")

    msg = None        # обычное сообщение для правок
    eph_id = None     # id эфемерного сообщения
    sent_len = 0      # сколько символов уже в завершённых обычных сообщениях
    first_edit_done = False
    last_edit = time.monotonic()
    typing = asyncio.create_task(_typing_loop(bot, chat_id))
    stream = None

    try:
        models = all_models()
        for mi, model in enumerate(models):
            try:
                stream = await open_stream(model, messages)
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                logger.warning("Не удалось начать стрим (%s): %s", model, err)
                if mi + 1 < len(models):
                    await asyncio.sleep(retry_delay(e))
        if stream is None:
            return None, err or "все модели недоступны", False

        async for piece, rpiece in iter_stream_deltas(stream):
            if handle and handle.stop.is_set():
                stopped = True
                break
            if rpiece:
                thinking += rpiece
            if piece:
                full += piece
            now = time.monotonic()

            if mode == "rich_draft":
                draft_md = ""
                if full.strip():
                    draft_md = full[:RICH_LIMIT]
                elif thinking.strip():
                    # только последние 2 строки размышлений
                    tail = "\n".join(thinking.strip().splitlines()[-2:])[-400:]
                    draft_md = "<tg-thinking>" + html.escape(tail) + "</tg-thinking>"
                if draft_md and (not first_edit_done or now - last_edit >= DRAFT_INTERVAL):
                    first_edit_done = True
                    last_edit = now
                    try:
                        await bot.send_rich_message_draft(
                            chat_id=chat_id,
                            draft_id=draft_id,
                            rich_message=InputRichMessage(markdown=draft_md),
                            message_thread_id=thread_id,
                            can_stop=True,
                        )
                    except Exception as e:
                        logger.warning("Rich-черновики недоступны (%s), простой черновик", e)
                        mode = "plain_draft"
                continue

            if mode == "plain_draft":
                if not first_edit_done or now - last_edit >= DRAFT_INTERVAL:
                    first_edit_done = True
                    last_edit = now
                    try:
                        await bot.send_message_draft(
                            chat_id=chat_id,
                            draft_id=draft_id,
                            text=full[:TG_LIMIT],
                            message_thread_id=thread_id,
                            can_stop=True,
                        )
                    except Exception as e:
                        logger.warning("Черновики сообщений недоступны (%s), правка сообщений", e)
                        mode = "edits"
                continue

            if mode == "ephemeral":
                due = (
                    (not first_edit_done and len(full) >= FIRST_EDIT_CHARS)
                    or now - last_edit >= EDIT_INTERVAL
                )
                if not due:
                    continue
                first_edit_done = True
                last_edit = now
                try:
                    if eph_id is None:
                        placeholder = await bot.send_message(
                            chat_id,
                            "…",
                            message_thread_id=thread_id,
                            ephemeral_message_parameters=eph,
                        )
                        track_sent(chat_id, placeholder)
                        eph_id = placeholder.ephemeral_message_id
                        if not eph_id:
                            await delete_message_quiet(bot, chat_id, placeholder.message_id)
                            raise RuntimeError("API не вернул ephemeral_message_id")
                    await bot.edit_ephemeral_message_text(
                        chat_id,
                        eph.receiver_user_id,
                        eph_id,
                        rich_message=InputRichMessage(markdown=full[:RICH_LIMIT]),
                    )
                except Exception as e:
                    logger.warning("Эфемерный стрим не удался (%s), публичные сообщения", e)
                    if eph_id is not None:
                        await delete_ephemeral_quiet(bot, chat_id, eph.receiver_user_id, eph_id)
                        eph_id = None
                    eph = None  # дальше отвечаем публично, не повторяем эфемерную отправку
                    mode = "edits"
                continue

            # mode == "edits": публичные сообщения в группах или запасной вариант
            if not full:
                continue
            if msg is None:
                msg = await bot.send_message(chat_id, "…", message_thread_id=thread_id)
                track_sent(chat_id, msg)
            due = (
                (not first_edit_done and len(full) >= FIRST_EDIT_CHARS)
                or now - last_edit >= EDIT_INTERVAL
            )
            if not due:
                continue
            first_edit_done = True
            last_edit = now
            cur_len = len(full) - sent_len
            if cur_len > CHUNK_SAFE:
                nl = full.rfind("\n", sent_len + CHUNK_SAFE - 300, sent_len + CHUNK_SAFE)
                cut = nl + 1 if nl != -1 else sent_len + CHUNK_SAFE
                if await edit_msg(msg, full[sent_len:cut]):
                    sent_len = cut
                    msg = await bot.send_message(chat_id, "…", message_thread_id=thread_id)
                    track_sent(chat_id, msg)
            else:
                await edit_msg(msg, full[sent_len:])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.warning("Ошибка стрима: %s", err)
    finally:
        typing.cancel()
        if handle:
            active_drafts.pop(draft_id, None)
        if stream is not None:
            try:
                await stream.close()
            except Exception:
                pass

    if not full:
        return None, err or "нейросеть вернула пустой ответ", stopped

    note = "\n\n⚠️ Ответ мог оборваться из-за ошибки соединения." if err else ""
    if stopped:
        note += "\n\n⏹ Генерация остановлена."
    plain_body = full + note
    rich_body = full + note + rich_suffix

    if eph_id is not None:
        if await _finish_ephemeral(bot, chat_id, eph, eph_id, thread_id, rich_body, plain_body):
            return full, err, stopped
        eph = None  # эфемерная отправка не удалась — дальше публично

    # завершаем открытое обычное сообщение, если стрим шёл правками
    if msg is not None:
        try:
            rest = plain_body[sent_len:]
            if rest:
                if len(rest) <= TG_LIMIT:
                    await edit_msg(msg, rest)
                else:
                    await edit_msg(msg, rest[:TG_LIMIT])
                    await send_plain(bot, chat_id, rest[TG_LIMIT:], thread_id=thread_id)
            else:
                await delete_message_quiet(bot, chat_id, msg.message_id)
            return full, err, stopped
        except Exception as e:
            logger.warning("Финальная правка не удалась (%s), отправка новых сообщений", e)

    if not await deliver_final(
        bot, chat_id, plain_body, rich_body, thread_id=thread_id, eph=eph
    ):
        return None, "не удалось отправить ответ", stopped
    return full, err, stopped


async def generate_and_reply(
    message: Message,
    messages: list[dict],
    history: list[dict],
    *,
    rich_suffix: str = "",
) -> None:
    answer, err, stopped = await send_streaming(message, messages, rich_suffix=rich_suffix)
    if answer is None and not stopped:
        answer, err = await ask_ai(messages)
        if answer and not await deliver_answer(message, answer, rich_suffix):
            answer = None
            err = "не удалось отправить ответ"
    if answer is None:
        history.pop()
        if stopped:
            await reply(message, "Генерация остановлена.")
        else:
            await reply(message, f"Не получилось получить ответ ({err}). Попробуй ещё раз.")
        return
    history.append({"role": "assistant", "content": answer})
    trim_history(history)


# ---------------- хендлеры ----------------

COMMANDS: list[tuple[str, str]] = [
    ("start", "Начало работы"),
    ("files", "Файлы памяти"),
    ("web", "Поиск в интернете: /web запрос"),
    ("news", "Свежие новости: /news тема"),
    ("clear", "Очистить историю диалога"),
    ("delete", "Удалить сообщения бота и историю"),
]


async def setup_commands(bot: Bot) -> None:
    private = [BotCommand(command=c, description=d) for c, d in COMMANDS]
    group = [
        BotCommand(command=c, description=d, is_ephemeral=EPHEMERAL_GROUPS)
        for c, d in COMMANDS
    ]
    try:
        await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning("Не удалось установить команды меню: %s", e)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    group_hint = (
        "\nВ группах отвечаю эфемерно — ответ виден только вам."
        if EPHEMERAL_GROUPS
        else ""
    )
    await reply(
        message,
        "Привет! Я бот с ИИ и долговременной памятью.\n\n"
        "Файлы памяти подключаются тегом [file-N] в сообщении, например:\n"
        "[file-1] Перескажи суть этого файла\n\n"
        "Поиск в интернете: [web] запрос или /web запрос\n"
        "Свежие новости: /news тема\n"
        "Запрос для поиска бот уточняет сам по истории диалога\n"
        "Во время ответа можно нажать «Остановить»\n"
        f"{group_hint}\n"
        "/files — список файлов с номерами\n"
        "/clear — очистить историю диалога\n"
        "/delete — удалить все сообщения в чате\n"
        "Редактор памяти: python memory.py"
    )


@dp.message(Command("files"))
async def cmd_files(message: Message) -> None:
    files = numbered_files()
    if not files:
        await reply(message, "Папка memory пуста. Добавь файлы через memory.py.")
        return
    lines = [f"[file-{i}] — {p.name}" for i, p in enumerate(files, 1)]
    await reply(
        message,
        "Файлы памяти:\n" + "\n".join(lines)
        + "\n\nУкажи нужные номера в сообщении: [file-1] или [file-1] [file-3]"
        + ("\nБез тега память не отправляется." if not SEND_ALL_WITHOUT_TAGS else "")
    )


@dp.message(Command("web"))
async def cmd_web(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await reply(message, "Использование: /web поисковый запрос")
        return
    await answer_with_web(message, parts[1].strip(), news=False)


@dp.message(Command("news"))
async def cmd_news(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await reply(message, "Использование: /news тема новостей")
        return
    await answer_with_web(message, parts[1].strip(), news=True)


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    histories.pop(message.from_user.id, None)
    await reply(message, "История диалога очищена.")


@dp.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    bot = message.bot
    chat_id = message.chat.id
    user_id = message.from_user.id
    cmd_id = message.message_id

    ids: set[int] = set()
    if message.chat.type == "private":
        # в личном чате ID последовательные — сканируем последние
        ids.update(chat_msgs.get(chat_id, []))
        ids.update(range(max(1, cmd_id - DELETE_SCAN), cmd_id))
    else:
        # в группах удаляем только свои сообщения, не чужие
        ids.update(own_msgs.get(chat_id, []))
    ids.discard(cmd_id)

    deleted = 0
    for mid in sorted(ids):
        if await delete_message_quiet(bot, chat_id, mid):
            deleted += 1
        else:
            await asyncio.sleep(0.03)  # лёгкий антифлуд

    eph_deleted = 0
    for eid in list(eph_msgs.get((chat_id, user_id), [])):
        if await delete_ephemeral_quiet(bot, chat_id, user_id, eid):
            eph_deleted += 1
    eph_msgs.pop((chat_id, user_id), None)

    if message.chat.type == "private":
        chat_msgs.pop(chat_id, None)
    else:
        own_msgs.pop(chat_id, None)
    histories.pop(user_id, None)
    await delete_message_quiet(bot, chat_id, cmd_id)

    try:
        await reply(
            message,
            f"Чат очищен: удалено {deleted + eph_deleted} сообщений.\n"
            "(сообщения старше 48 часов Telegram удалять не даёт)",
        )
    except Exception:
        logger.warning("Не удалось отправить подтверждение /delete")


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not (AI_API_KEY or AI_AUTH_TOKEN):
        await reply(message, "Fill in AI_API_KEY in the .env file")
        return
    if message.from_user is None:  # посты в каналах — некому отвечать
        return
    user_id = message.from_user.id
    if WEB_TAG_RE.search(message.text):
        query = strip_all_tags(message.text)
        if query:
            await answer_with_web(message, query, news=False)
            return
    memory = load_memory_by_tags(message.text)
    text = strip_tags(message.text)
    if not text:
        if memory:  # только теги [file-N] — просим пересказ
            text = "Кратко перескажи содержание подключённых файлов."
        else:
            return
    history = histories.setdefault(user_id, [])
    history.append({"role": "user", "content": text})
    messages = build_messages(user_id, memory)

    await generate_and_reply(message, messages, history)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith(("сюда", "paste_your")):
        raise SystemExit("Fill in TELEGRAM_BOT_TOKEN in the .env file")
    MEMORY_DIR.mkdir(exist_ok=True)
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await setup_commands(bot)
    logger.info(
        "Бот запущен. Провайдер: %s, модель: %s, API: %s",
        AI_PROVIDER, AI_MODEL, AI_API_URL,
    )
    try:
        await dp.start_polling(bot)
    finally:
        if client is not None:
            await client.close()
        if anthropic_client is not None:
            await anthropic_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
