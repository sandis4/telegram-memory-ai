import asyncio
import html
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

AI_API_URL = os.getenv("AI_API_URL", "https://openrouter.ai/api/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
# Запасные модели через запятую: используются по очереди при rate-limit основной
AI_MODEL_FALLBACK = [m.strip() for m in os.getenv("AI_MODEL_FALLBACK", "").split(",") if m.strip()]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Ускоряет «думающие» модели (gpt-oss, deepseek-r1): low/medium/high. Пусто — не отправлять.
AI_REASONING = os.getenv("AI_REASONING", "")

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
DRAFT_INTERVAL = 1.0  # секунды между обновлениями rich-черновика
FIRST_EDIT_CHARS = 50  # первый показ текста почти сразу
FILE_TAG_RE = re.compile(r"\[file-(\d+)\]", re.IGNORECASE)
WEB_TAG_RE = re.compile(r"\[(?:web|поиск|интернет)\]", re.IGNORECASE)

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

client = AsyncOpenAI(
    base_url=AI_API_URL,
    api_key=AI_API_KEY,
    timeout=httpx.Timeout(90.0, connect=10.0),  # не висеть минутами при сбое
    max_retries=0,  # ретраи делаем сами, с учётом Retry-After
)
EXTRA_BODY = {"reasoning": {"effort": AI_REASONING}} if AI_REASONING else {}
dp = Dispatcher()

histories: dict[int, list[dict]] = {}

# трекинг сообщений чата для /delete
CHAT_MSG_LIMIT = 800
DELETE_SCAN = 400  # сколько последних ID сканировать в личном чате
chat_msgs: dict[int, deque[int]] = {}


def track(chat_id: int, *message_ids) -> None:
    dq = chat_msgs.setdefault(chat_id, deque(maxlen=CHAT_MSG_LIMIT))
    for mid in message_ids:
        if mid:
            dq.append(mid)


@dp.message.middleware()
async def _track_incoming(handler, event: Message, data):
    track(event.chat.id, event.message_id)
    return await handler(event, data)


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


async def answer_with_web(message: Message, query: str, news: bool = False) -> None:
    """Ищет в интернете и отвечает нейросетью с опорой на найденное."""
    query = strip_all_tags(query)
    user_id = message.from_user.id
    history = histories.setdefault(user_id, [])
    prefix = "Новости: " if news else "Поиск: "
    history.append({"role": "user", "content": prefix + query})

    status = await message.answer("Ищу новости..." if news else "Ищу в интернете...")
    results, err = await web_search(query, news)

    extra = ""
    if results:
        kind = "свежие новостные статьи" if news else "результаты веб-поиска"
        extra = (
            f"\nНиже — {kind} по запросу «{query}». "
            "Опирайся на них при ответе и указывай источники ссылками.\n\n"
            + format_search(results, news)
        )
        if not news and results[0].get("href"):
            try:  # полный текст первой статьи
                ext = await asyncio.to_thread(_ddg_extract, results[0]["href"])
                content = (ext.get("content") or "").strip()[:WEB_EXTRACT_CHARS]
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

    try:
        await status.delete()
    except Exception:
        pass

    messages = build_messages(user_id, "", extra)
    answer, ai_err = await send_streaming(message.bot, message.chat.id, messages)
    if answer is None:
        answer, ai_err = await ask_ai(messages, attempts=2)
    if answer is None:
        history.pop()
        await message.answer(f"Не получилось получить ответ ({ai_err}). Попробуй ещё раз.")
        return

    history.append({"role": "assistant", "content": answer})
    trim_history(history)


def trim_history(history: list[dict]) -> None:
    """Держит историю короткой: меньше токенов — быстрее ответ."""
    for m in history:
        if len(m["content"]) > MAX_MSG_CHARS:
            m["content"] = m["content"][:MAX_MSG_CHARS] + "…"
    total = sum(len(m["content"]) for m in history)
    while history and (len(history) > MAX_HISTORY * 2 or total > MAX_HISTORY_CHARS):
        total -= len(history[0]["content"])
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


async def edit_msg(msg: Message, text: str) -> None:
    """Правит сообщение с HTML-разметкой; если разметка битая — обычным текстом."""
    try:
        await msg.edit_text(md_to_html(text), parse_mode="HTML")
    except Exception:
        try:
            await msg.edit_text(text[:TG_LIMIT])
        except Exception:
            pass


# ---------------- Rich Messages (Bot API 10.1+) ----------------

tg_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))


async def tg_call(method: str, **payload):
    """Прямой вызов метода Bot API (для методов, которых ещё нет в aiogram)."""
    resp = await tg_http.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        json=payload,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"{method}: unknown error"))
    return data.get("result")


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


async def ask_ai(messages: list[dict], attempts: int = 4) -> tuple[str | None, str]:
    """Нестримовый запрос; модели перебираются по очереди при сбоях."""
    models = all_models()
    last_err = ""
    wait = 3.0
    for attempt in range(attempts):
        model = models[attempt % len(models)]
        try:
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


async def send_streaming(bot: Bot, chat_id: int, messages: list[dict]) -> tuple[str | None, str]:
    """Стримит ответ как черновик-статью (Rich Messages), финализирует sendRichMessage.
    Если API/чат не поддерживает rich — откат на обычные сообщения с лимитом 4096."""
    full = ""
    thinking = ""    # размышления reasoning-моделей — показываются в черновике
    err = ""
    rich_ok = True
    draft_id = random.randint(1, 2**31 - 1)
    msg = None        # сообщение для обычного режима
    sent_len = 0      # сколько символов уже в завершённых сообщениях (обычный режим)
    first_edit_done = False
    last_edit = time.monotonic()
    typing = asyncio.create_task(_typing_loop(bot, chat_id))
    try:
        stream = None
        models = all_models()
        for mi, model in enumerate(models):
            try:
                stream = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    extra_body=EXTRA_BODY,
                )
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                logger.warning("Не удалось начать стрим (%s): %s", model, err)
                if mi + 1 < len(models):
                    await asyncio.sleep(retry_delay(e))
        if stream is None:
            return None, err or "все модели недоступны"
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            rpiece = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rpiece:
                thinking += rpiece
            piece = getattr(delta, "content", None)
            if piece:
                full += piece
            now = time.monotonic()

            if rich_ok:
                if full.strip():
                    draft_md = full[:RICH_LIMIT]
                elif thinking.strip():
                    # только последние 2 строки размышлений
                    tail = "\n".join(thinking.strip().splitlines()[-2:])[-400:]
                    draft_md = "<tg-thinking>" + html.escape(tail) + "</tg-thinking>"
                else:
                    draft_md = ""
                if draft_md and (
                    not first_edit_done or now - last_edit >= DRAFT_INTERVAL
                ):
                    first_edit_done = True
                    last_edit = now
                    try:
                        await tg_call(
                            "sendRichMessageDraft",
                            chat_id=chat_id,
                            draft_id=draft_id,
                            rich_message={"markdown": draft_md},
                        )
                        continue
                    except Exception as e:
                        logger.warning("Rich-черновики недоступны (%s), обычные сообщения", e)
                        rich_ok = False

            if rich_ok or not piece:
                continue
            if msg is None:
                msg = await bot.send_message(chat_id, "…")
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
                await edit_msg(msg, full[sent_len:cut])
                sent_len = cut
                msg = await bot.send_message(chat_id, "…")
            else:
                await edit_msg(msg, full[sent_len:])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.warning("Ошибка стрима: %s", err)
    finally:
        typing.cancel()

    if not full:
        return None, err or "нейросеть вернула пустой ответ"

    body = full + ("\n\n⚠️ Ответ мог оборваться из-за ошибки соединения." if err else "")

    if rich_ok:
        try:
            for part in split_for_rich(body):
                await tg_call(
                    "sendRichMessage",
                    chat_id=chat_id,
                    rich_message={"markdown": part},
                )
            return full, err
        except Exception as e:
            logger.warning("sendRichMessage не удался (%s), отправка текстом", e)

    if msg is None:
        msg = await bot.send_message(chat_id, "…")
    rest = body[sent_len:]
    if rest:
        await edit_msg(msg, rest[:TG_LIMIT])
        for i in range(TG_LIMIT, len(rest), TG_LIMIT):
            await bot.send_message(chat_id, rest[i : i + TG_LIMIT])
    return full, err


# ---------------- хендлеры ----------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот с ИИ и долговременной памятью.\n\n"
        "Файлы памяти подключаются тегом [file-N] в сообщении, например:\n"
        "[file-1] Перескажи суть этого файла\n\n"
        "Поиск в интернете: [web] запрос или /web запрос\n"
        "Свежие новости: /news тема\n\n"
        "/files — список файлов с номерами\n"
        "/clear — очистить историю диалога\n"
        "/delete — удалить все сообщения в чате\n"
        "Редактор памяти: python memory.py"
    )


@dp.message(Command("files"))
async def cmd_files(message: Message) -> None:
    files = numbered_files()
    if not files:
        await message.answer("Папка memory пуста. Добавь файлы через memory.py.")
        return
    lines = [f"[file-{i}] — {p.name}" for i, p in enumerate(files, 1)]
    await message.answer(
        "Файлы памяти:\n" + "\n".join(lines)
        + "\n\nУкажи нужные номера в сообщении: [file-1] или [file-1] [file-3]"
        + ("\nБез тега память не отправляется." if not SEND_ALL_WITHOUT_TAGS else "")
    )


@dp.message(Command("web"))
async def cmd_web(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /web поисковый запрос")
        return
    await answer_with_web(message, parts[1].strip(), news=False)


@dp.message(Command("news"))
async def cmd_news(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /news тема новостей")
        return
    await answer_with_web(message, parts[1].strip(), news=True)


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    histories.pop(message.from_user.id, None)
    await message.answer("История диалога очищена.")


async def delete_quiet(bot: Bot, chat_id: int, message_id: int) -> bool:
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


@dp.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    bot = message.bot
    chat_id = message.chat.id
    cmd_id = message.message_id
    ids = set(chat_msgs.get(chat_id, []))
    if message.chat.type == "private":
        # в личном чате ID последовательные — сканируем последние
        ids.update(range(max(1, cmd_id - DELETE_SCAN), cmd_id))
    ids.discard(cmd_id)

    deleted = 0
    for mid in sorted(ids):
        if await delete_quiet(bot, chat_id, mid):
            deleted += 1
        else:
            await asyncio.sleep(0.03)  # лёгкий антифлуд

    chat_msgs.pop(chat_id, None)
    histories.pop(message.from_user.id, None)
    await bot.delete_message(chat_id, cmd_id)
    done = await bot.send_message(
        chat_id,
        f"Чат очищен: удалено {deleted} сообщений.\n"
        "(сообщения старше 48 часов Telegram удалять не даёт)",
    )
    track(chat_id, done.message_id)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not AI_API_KEY or AI_API_KEY.startswith("сюда"):
        await message.answer("Заполни AI_API_KEY в файле .env")
        return
    user_id = message.from_user.id
    if WEB_TAG_RE.search(message.text):
        query = strip_all_tags(message.text)
        if query:
            await answer_with_web(message, query, news=False)
            return
    memory = load_memory_by_tags(message.text)
    history = histories.setdefault(user_id, [])
    history.append({"role": "user", "content": strip_tags(message.text)})
    messages = build_messages(user_id, memory)

    answer, err = await send_streaming(message.bot, message.chat.id, messages)
    if answer is None:
        answer, err = await ask_ai(messages, attempts=2)
    if answer is None:
        history.pop()
        await message.answer(f"Не получилось получить ответ ({err}). Попробуй ещё раз.")
        return

    history.append({"role": "assistant", "content": answer})
    trim_history(history)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("сюда"):
        raise SystemExit("Заполни TELEGRAM_BOT_TOKEN в файле .env")
    MEMORY_DIR.mkdir(exist_ok=True)
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info("Бот запущен. Модель: %s, API: %s", AI_MODEL, AI_API_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
