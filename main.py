import asyncio
import logging
import html
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

# --- ИМПОРТЫ ИЗ MAXAPI ---
from maxapi import Bot, Dispatcher
from maxapi.types import (
    BotStarted,
    CallbackButton,
    InputMedia,
    MessageButton,
    MessageCallback,
    MessageCreated,
)
from maxapi.enums.parse_mode import ParseMode
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

# --- ИМПОРТЫ ИЗ НАШИХ МОДУЛЕЙ ---
from config import Settings
from database import AppDB
from ai_client import PromptAgentClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "static" / "img" / "intro.png"

# --- ИНИЦИАЛИЗАЦИЯ ---
settings = Settings.from_env()
bot = Bot(settings.max_api_token)
dp = Dispatcher()
db = AppDB()
assistant_client = PromptAgentClient(settings, db)


CALLBACK_BACK = "back"
CALLBACK_ASK = "menu:ask"
CALLBACK_GALLERY = "menu:gallery"
CALLBACK_PORTFOLIO = "menu:portfolio"
CALLBACK_GRANT_CHECK = "menu:grant_check"
CALLBACK_CENTERS_SEARCH = "menu:centers_search"
CALLBACK_CENTERS_PAGE_PREFIX = "centers:page:"
CENTERS_PAGE_SIZE = 5
PROJECT_PAYLOAD_MAP = {
    "project:bp": ("Большая перемена", "BP"),
    "project:tvoyhod": ("Твой Ход", "TVOYHOD"),
    "project:forums": ("Форумы", "FORUMS"),
    "project:grants": ("Гранты", "GRANTS"),
}
YOUTH_POLICY_DB_PATH = BASE_DIR / "youth_policy.db"
SEARCH_RESULTS_CACHE: dict[str, dict] = {}


def get_back_kb():
    """Создает клавиатуру с кнопкой 'Назад'"""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


def search_institutions_by_locality(locality: str, max_results: int = 200) -> list[dict]:
    """Ищет молодежные учреждения по населенному пункту/городу."""
    query = locality.strip()
    if not query or not YOUTH_POLICY_DB_PATH.exists():
        return []

    with sqlite3.connect(YOUTH_POLICY_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = conn.execute(
            """
            SELECT name, address_phone, head_name, web_resources, activity_directions
            FROM institutions
            """
        ).fetchall()

    normalized_query = _normalize_for_match(query)
    if not normalized_query:
        return []

    query_variants = _expand_locality_variants(normalized_query)

    # 1) Точное включение по нормализованным строкам (unicode-safe, без ограничений SQLite NOCASE)
    direct_matches: list[sqlite3.Row] = []
    for row in all_rows:
        name = _normalize_for_match(row["name"] or "")
        address_phone = _normalize_for_match(row["address_phone"] or "")
        combined = f"{name} {address_phone}".strip()
        if not combined:
            continue
        if any(variant in combined for variant in query_variants):
            direct_matches.append(row)

    if direct_matches:
        logger.info(
            "CENTER_SEARCH query=%s normalized=%s direct_matches=%s",
            locality,
            normalized_query,
            len(direct_matches),
        )
        return [dict(row) for row in direct_matches[:max_results]]

    # 2) Fuzzy fallback для опечаток/вариантов написания.
    ranked: list[tuple[float, sqlite3.Row]] = []
    for row in all_rows:
        name = _normalize_for_match(row["name"] or "")
        address_phone = _normalize_for_match(row["address_phone"] or "")
        combined = f"{name} {address_phone}".strip()
        if not combined:
            continue

        score = max(
            max(SequenceMatcher(None, variant, name).ratio() for variant in query_variants),
            max(SequenceMatcher(None, variant, address_phone).ratio() for variant in query_variants),
            max(SequenceMatcher(None, variant, combined).ratio() for variant in query_variants),
        )
        if score >= 0.55:
            ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)
    logger.info(
        "CENTER_SEARCH query=%s normalized=%s fuzzy_matches=%s",
        locality,
        normalized_query,
        len(ranked),
    )
    return [dict(row) for _, row in ranked[:max_results]]


def _normalize_for_match(text: str) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9\s-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _expand_locality_variants(normalized_query: str) -> list[str]:
    variants = {normalized_query}
    if normalized_query == "челны":
        variants.add("набережные челны")
    if normalized_query.endswith("ь"):
        variants.add(normalized_query[:-1])
    if "г " in normalized_query:
        variants.add(normalized_query.replace("г ", "").strip())
    return [v for v in variants if v]


def format_institutions_response(locality: str, rows: list[dict], total_count: int, page: int) -> str:
    if not rows:
        return (
            f"По запросу «{locality}» ничего не нашлось.\n"
            "Попробуйте другой город/населенный пункт или уточните написание."
        )

    if page == 0:
        summary = f"Найдено: {total_count}. Показаны первые 5."
    else:
        start_idx = page * CENTERS_PAGE_SIZE + 1
        end_idx = min(total_count, (page + 1) * CENTERS_PAGE_SIZE)
        summary = f"Найдено: {total_count}. Показаны {start_idx}-{end_idx}."

    parts = [f"Нашел организации по запросу «{locality}»:\n{summary}"]
    for idx, row in enumerate(rows, start=1):
        name = _clean_field_value(row.get("name"), fallback="Без названия")
        address_phone = _clean_field_value(row.get("address_phone"))
        head_name = _clean_field_value(row.get("head_name"))
        web_resources = _clean_field_value(row.get("web_resources"))
        parts.append(
            f"{idx}) {name}\n"
            f"   Адрес/контакты: {address_phone}\n"
            f"   Руководитель: {head_name}\n"
            f"   Ресурсы: {web_resources}"
        )
    return "\n\n".join(parts)


def _clean_field_value(value: str | None, fallback: str = "Не указано") -> str:
    if not value:
        return fallback
    cleaned = str(value).replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    return cleaned or fallback


def get_centers_page_kb(page: int, total_count: int):
    kb = InlineKeyboardBuilder()
    total_pages = max(1, (total_count + CENTERS_PAGE_SIZE - 1) // CENTERS_PAGE_SIZE)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(CallbackButton(text="⬅️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(CallbackButton(text="➡️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page + 1}"))
    if nav_buttons:
        kb.row(*nav_buttons)

    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


async def send_centers_results_page(event, user_id: str, page: int):
    cached = SEARCH_RESULTS_CACHE.get(user_id)
    if not cached:
        text = "Поиск не найден в истории. Введите город или населенный пункт заново."
        if isinstance(event, MessageCallback) and event.message is not None:
            await event.answer()
            await event.message.edit(text=text, attachments=[get_back_kb()])
        else:
            await event.message.answer(text, attachments=[get_back_kb()])
        return

    locality = cached["query"]
    results = cached["results"]
    total_count = len(results)
    total_pages = max(1, (total_count + CENTERS_PAGE_SIZE - 1) // CENTERS_PAGE_SIZE)
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * CENTERS_PAGE_SIZE
    end = start + CENTERS_PAGE_SIZE
    page_rows = results[start:end]

    text = format_institutions_response(locality, page_rows, total_count=total_count, page=safe_page)
    markup = get_centers_page_kb(safe_page, total_count)

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[markup])
    else:
        await event.message.answer(text, attachments=[markup])


async def send_welcome(event, user_id):
    """Отправляет приветственное сообщение и главное меню"""
    assistant_client.reset_user(user_id)
    assistant_client.set_state(user_id, "DEFAULT")
    text = (
        "Привет! 👋 \nЯ — твой ИИ-помощник по возможностям для молодёжи в Татарстане и России. 🧭 \n\n"
        "Выбери нужный раздел в меню ниже.\n\n"
        "\n_Продолжая пользоваться ботом, ты принимаешь [Соглашение на обработку персональных данных](https://clck.su/NeKDn)._"
    )
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Задать вопрос", payload=CALLBACK_ASK))
    kb.row(CallbackButton(text="Поиск молодежных центров", payload=CALLBACK_CENTERS_SEARCH))
    kb.row(CallbackButton(text="Галерея (в разработке)", payload=CALLBACK_GALLERY))
    kb.row(CallbackButton(text="Портфолио (в разработке)", payload=CALLBACK_PORTFOLIO))
    kb.row(
        CallbackButton(
            text="Проверить грантовую заявку (в разработке)",
            payload=CALLBACK_GRANT_CHECK,
        )
    )

    markup = kb.as_markup()
    chat_id = event.chat_id if hasattr(event, 'chat_id') else event.message.recipient.chat_id

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text="👇 Главное меню:", attachments=[markup], format=ParseMode.MARKDOWN)
        return

    if IMAGE_PATH.exists():
        try:
            await event.bot.send_message(chat_id=chat_id, text=text, attachments=[InputMedia(str(IMAGE_PATH))],
                                         format=ParseMode.MARKDOWN)
            await event.bot.send_message(chat_id=chat_id, text="👇 Главное меню:", attachments=[markup],
                                         format=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Ошибка отправки медиа: {e}")
            await event.bot.send_message(chat_id=chat_id, text=text, attachments=[markup], format=ParseMode.MARKDOWN)
    else:
        await event.bot.send_message(chat_id=chat_id, text=text, attachments=[markup], format=ParseMode.MARKDOWN)


async def send_project_menu(event, user_id):
    """Отправляет меню выбора проекта"""
    assistant_client.set_state(user_id, "SELECT_PROJECT")
    text = "Выбери проект, по которому хочешь задать вопрос:"
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="Большая перемена", payload="project:bp"),
        CallbackButton(text="Твой Ход", payload="project:tvoyhod"),
    )
    kb.row(
        CallbackButton(text="Форумы", payload="project:forums"),
        CallbackButton(text="Гранты (в разработке)", payload="project:grants"),
    )
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))

    markup = kb.as_markup()
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[markup])
        return

    await event.message.answer(text, attachments=[markup])


async def send_centers_search_prompt(event, user_id):
    """Переводит пользователя в режим поиска молодежных центров."""
    assistant_client.set_state(user_id, "SEARCH_CENTER")
    SEARCH_RESULTS_CACHE.pop(user_id, None)
    text = "Введите город или населенный пункт, и я найду подходящие молодежные центры."
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[get_back_kb()])
        return
    await event.message.answer(text, attachments=[get_back_kb()])


async def handle_ai_stream(event, user_id, text, intent):
    """Обработка стриминга ответа от нейросети"""
    status_msg = await event.message.answer("⏳ *Изучаю запрос...*", format=ParseMode.MARKDOWN)

    update_count = 0
    UPDATE_STEP = 10
    partial_text = ""

    try:
        # Используем генератор ask_stream из ai_client.py
        for chunk_text in assistant_client.ask_stream(user_id, text, explicit_intent=intent):
            partial_text = chunk_text
            update_count += 1
            if update_count % UPDATE_STEP == 0:
                try:
                    await status_msg.message.edit(text=partial_text)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Ошибка при стриминге: {e}")
        partial_text = "Произошла ошибка при генерации ответа."

    try:
        await status_msg.message.edit(text=partial_text, format=ParseMode.MARKDOWN)
        await event.message.answer("Спроси еще или вернись назад:", attachments=[get_back_kb()])
    except Exception:
        pass


# --- ХЕНДЛЕРЫ ---

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = str(event.user_id or event.chat_id)
    db.log_interaction(user_id, "COMMAND", "BotStarted")
    await send_welcome(event, user_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    body = event.message.body
    text = ""

    if isinstance(body, str):
        text = body
    else:
        text = str(getattr(body, "text", getattr(body, "caption", "")))

    if text == "None": text = ""
    text = text.strip()

    sender = event.message.sender
    user_id = str(sender.user_id if sender else event.message.recipient.chat_id)
    db.upsert_user(user_id, sender)
    state = assistant_client.get_state(user_id)

    if not text: return

    if text.lower() in ["/start", "старт", "начать"]:
        await send_welcome(event, user_id)
        return

    # Логика кнопки "Назад" с соблюдением вложенности
    if text.lower() == "назад":
        if state == "CHATTING":
            await send_project_menu(event, user_id)
        else:
            await send_welcome(event, user_id)
        return

    if text in ["Галерея (в разработке)", "Портфолио (в разработке)", "Проверить грантовую заявку (в разработке)"]:
        await event.message.answer("Этот раздел находится в разработке 🛠", attachments=[get_back_kb()])
        return

    if state == "DEFAULT":
        if text == "Задать вопрос":
            await send_project_menu(event, user_id)
        elif text == "Поиск молодежных центров":
            await send_centers_search_prompt(event, user_id)
        else:
            await event.message.answer("Воспользуйтесь кнопками меню.")
        return

    project_map = {
        "Большая перемена": "BP",
        "Твой Ход": "TVOYHOD",
        "Форумы": "FORUMS",
        "Гранты": "GRANTS"
    }

    if state == "SELECT_PROJECT":
        if text in project_map:
            assistant_client.set_project(user_id, project_map[text])
            assistant_client.set_state(user_id, "CHATTING")
            await event.message.answer(f"Я готов отвечать по теме «{text}». Жду твой вопрос!",
                                       attachments=[get_back_kb()])
        else:
            await event.message.answer("Выберите проект из списка.", attachments=[get_back_kb()])
        return

    if state == "CHATTING":
        try:
            await event.bot.send_chat_action(chat_id=user_id, action="typing")
        except:
            pass
        current_project = assistant_client.get_project(user_id)
        asyncio.create_task(handle_ai_stream(event, user_id, text, current_project))
        return

    if state == "SEARCH_CENTER":
        matches = search_institutions_by_locality(text)
        SEARCH_RESULTS_CACHE[user_id] = {"query": text, "results": matches}
        await send_centers_results_page(event, user_id, page=0)
        return


@dp.message_callback()
async def on_callback(event: MessageCallback):
    payload = (event.callback.payload or "").strip().lower()
    user_id = str(event.callback.user.user_id)
    state = assistant_client.get_state(user_id)

    if payload == CALLBACK_BACK:
        if state == "CHATTING":
            await send_project_menu(event, user_id)
        else:
            await send_welcome(event, user_id)
        return

    if payload == CALLBACK_ASK:
        await send_project_menu(event, user_id)
        return

    if payload == CALLBACK_CENTERS_SEARCH:
        await send_centers_search_prompt(event, user_id)
        return

    if payload.startswith(CALLBACK_CENTERS_PAGE_PREFIX):
        try:
            page = int(payload.split(":")[-1])
        except Exception:
            page = 0
        await send_centers_results_page(event, user_id, page=page)
        return

    if payload in {CALLBACK_GALLERY, CALLBACK_PORTFOLIO, CALLBACK_GRANT_CHECK}:
        if event.message is not None:
            await event.answer()
            await event.message.edit(
                text="Этот раздел находится в разработке 🛠",
                attachments=[get_back_kb()],
            )
        return

    if payload in PROJECT_PAYLOAD_MAP:
        project_title, project_intent = PROJECT_PAYLOAD_MAP[payload]
        assistant_client.set_project(user_id, project_intent)
        assistant_client.set_state(user_id, "CHATTING")
        if event.message is not None:
            await event.answer()
            await event.message.edit(
                text=f"Я готов отвечать по теме «{project_title}». Жду твой вопрос!",
                attachments=[get_back_kb()],
            )
        return


async def main():
    logger.info("Бот запущен на библиотеке maxapi (Long Polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())