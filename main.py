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
CALLBACK_EVENTS = "menu:events"  # Константа для нового раздела
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


def get_main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Задать вопрос", payload=CALLBACK_ASK))
    kb.row(CallbackButton(text="📅 События недели", payload=CALLBACK_EVENTS))
    kb.row(CallbackButton(text="Поиск молодежных центров", payload=CALLBACK_CENTERS_SEARCH))
    kb.row(CallbackButton(text="Галерея (в разработке)", payload=CALLBACK_GALLERY))
    kb.row(CallbackButton(text="Портфолио (в разработке)", payload=CALLBACK_PORTFOLIO))
    kb.row(CallbackButton(text="Проверить грантовую заявку", payload=CALLBACK_GRANT_CHECK))
    return kb.as_markup()


async def show_main_menu_inline(event: MessageCallback, user_id: str):
    assistant_client.reset_user(user_id)
    assistant_client.set_state(user_id, "DEFAULT")
    await event.answer()
    await event.message.edit(text="👇 Главное меню:", attachments=[get_main_menu_kb()])

# --- ФУНКЦИИ ПОИСКА ЦЕНТРОВ ---

def search_institutions_by_locality(locality: str, max_results: int = 200) -> list[dict]:
    query = locality.strip()
    if not query or not YOUTH_POLICY_DB_PATH.exists():
        return []

    with sqlite3.connect(YOUTH_POLICY_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = conn.execute(
            "SELECT name, address_phone, head_name, web_resources, activity_directions FROM institutions"
        ).fetchall()

    normalized_query = _normalize_for_match(query)
    if not normalized_query: return []
    query_variants = _expand_locality_variants(normalized_query)

    direct_matches = []
    for row in all_rows:
        name = _normalize_for_match(row["name"] or "")
        address_phone = _normalize_for_match(row["address_phone"] or "")
        combined = f"{name} {address_phone}".strip()
        if any(variant in combined for variant in query_variants):
            direct_matches.append(row)

    if direct_matches:
        return [dict(row) for row in direct_matches[:max_results]]

    ranked = []
    for row in all_rows:
        name = _normalize_for_match(row["name"] or "")
        combined = f"{name} {_normalize_for_match(row['address_phone'] or '')}".strip()
        score = max(SequenceMatcher(None, variant, combined).ratio() for variant in query_variants)
        if score >= 0.55: ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [dict(row) for _, row in ranked[:max_results]]


def _normalize_for_match(text: str) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9\s-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _expand_locality_variants(normalized_query: str) -> list[str]:
    variants = {normalized_query}
    if normalized_query == "челны": variants.add("набережные челны")
    if normalized_query.endswith("ь"): variants.add(normalized_query[:-1])
    if "г " in normalized_query: variants.add(normalized_query.replace("г ", "").strip())
    return [v for v in variants if v]


def format_institutions_response(locality: str, rows: list[dict], total_count: int, page: int) -> str:
    if not rows:
        return f"По запросу «{locality}» ничего не нашлось. Попробуйте другой город."
    summary = f"Найдено: {total_count}. Стр. {page + 1}"
    parts = [f"📍 Организации по запросу «{locality}»:\n{summary}"]
    for idx, row in enumerate(rows, start=1):
        parts.append(
            f"{idx}) {row.get('name', 'Без названия')}\n"
            f"   Адрес: {row.get('address_phone', 'Не указан')}\n"
            f"   Ресурсы: {row.get('web_resources', 'Нет информации')}"
        )
    return "\n\n".join(parts)


def _clean_field_value(value: str | None, fallback: str = "Не указано") -> str:
    if not value: return fallback
    cleaned = str(value).replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback


def get_centers_page_kb(page: int, total_count: int):
    kb = InlineKeyboardBuilder()
    total_pages = (total_count + CENTERS_PAGE_SIZE - 1) // CENTERS_PAGE_SIZE
    nav_buttons = []
    if page > 0:
        nav_buttons.append(CallbackButton(text="⬅️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(CallbackButton(text="➡️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page + 1}"))
    if nav_buttons: kb.row(*nav_buttons)
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


async def send_centers_results_page(event, user_id: str, page: int):
    cached = SEARCH_RESULTS_CACHE.get(user_id)
    if not cached:
        if hasattr(event, "message") and event.message is not None:
            await event.message.answer(text="Введите город заново:", attachments=[get_back_kb()])
        return
    results = cached["results"]
    total = len(results)
    start = page * CENTERS_PAGE_SIZE
    page_rows = results[start:start + CENTERS_PAGE_SIZE]
    text = format_institutions_response(cached["query"], page_rows, total, page)
    markup = get_centers_page_kb(page, total)
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.message.edit(text=text, attachments=[markup])
    else:
        if hasattr(event, "message") and event.message is not None:
            await event.message.answer(text=text, attachments=[markup])

# --- ОСНОВНЫЕ РАЗДЕЛЫ ---

async def send_events_list(event, user_id):
    """Описание мероприятий на текущую неделю"""
    text = (
        "📅 **События этой недели (27 апреля – 3 мая)**\n\n"
        "🔹 **27.04 (Пн)** — Старт регистрации на форум «ШУМ». Если ты про медиа и контент — это твой шанс!\n"
        "🔹 **28.04 (Вт)** — Вебинар Росмолодежи: «Как не завалить грантовую заявку». В 18:00 онлайн.\n"
        "🔹 **30.04 (Чт)** — Дедлайн подачи заявок в трек «Делаю» проекта **Твой Ход**. Последний день!\n"
        "🔹 **01.05 (Пт)** — Фестиваль уличной культуры в экстрим-парке «УРАМ» (Казань). Контесты и мастер-классы.\n"
        "🔹 **02.05 (Сб)** — Нетворкинг-встреча в Иннополисе: обсуждаем AI и будущее молодежных стартапов.\n\n"
        "📍 Следи за обновлениями в официальных сообществах молодежной политики Татарстана!"
    )
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[get_back_kb()], format=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=str(user_id), text=text, attachments=[get_back_kb()], format=ParseMode.MARKDOWN)


async def send_welcome(event, user_id):
    """Отправляет приветственное сообщение и главное меню"""
    assistant_client.reset_user(user_id)
    assistant_client.set_state(user_id, "DEFAULT")
    text = (
        "Привет! 👋\n"
        "Я — твой ИИ-помощник по возможностям для молодёжи в Татарстане и России. 🧭\n\n"
        "Выбери нужный раздел в меню ниже.\n\n"
        "Продолжая пользоваться ботом, ты принимаешь "
        "[Соглашение на обработку персональных данных]"
        "(https://xn-----6kcca1a0clhajkadginefbh2i.xn--p1ai/%D0%A1%D0%BE%D0%B3%D0%BB%D0%B0%D1%81%D0%B8%D0%B5%20%D0%BD%D0%B0%20%D0%9E%D0%9F%D0%94.pdf)."
    )
    markup = get_main_menu_kb()
    chat_id = event.chat_id if hasattr(event, 'chat_id') else event.message.recipient.chat_id

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
    if IMAGE_PATH.exists():
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                attachments=[InputMedia(str(IMAGE_PATH))],
                format=ParseMode.MARKDOWN,
            )
        except Exception:
            await bot.send_message(chat_id=chat_id, text=text, format=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=chat_id, text=text, format=ParseMode.MARKDOWN)

    await bot.send_message(chat_id=chat_id, text="👇 Главное меню:", attachments=[markup], format=ParseMode.MARKDOWN)


async def send_project_menu(event, user_id):
    assistant_client.set_state(user_id, "SELECT_PROJECT")
    text = "Выбери проект, по которому хочешь задать вопрос:"
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Большая перемена", payload="project:bp"), CallbackButton(text="Твой Ход", payload="project:tvoyhod"))
    kb.row(CallbackButton(text="Форумы", payload="project:forums"), CallbackButton(text="Гранты", payload="project:grants"))
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[kb.as_markup()])
    else:
        await event.message.answer(text, attachments=[kb.as_markup()])


async def send_centers_search_prompt(event, user_id):
    assistant_client.set_state(user_id, "SEARCH_CENTER")
    if isinstance(event, MessageCallback) and event.message is not None:
        chat_id = str(event.message.recipient.chat_id)
        assistant_client.set_state(chat_id, "SEARCH_CENTER")
    SEARCH_RESULTS_CACHE.pop(user_id, None)
    text = "Введите город или населенный пункт, и я найду подходящие молодежные центры."
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[get_back_kb()])
        return
    await event.message.answer(text, attachments=[get_back_kb()])


async def handle_ai_stream(event, user_id, text, intent):
    status_msg = await event.message.answer("⏳ *Изучаю запрос...*", format=ParseMode.MARKDOWN)
    partial_text = ""
    try:
        for chunk_text in assistant_client.ask_stream(user_id, text, explicit_intent=intent):
            partial_text = chunk_text
        await status_msg.message.edit(text=partial_text, format=ParseMode.MARKDOWN)
        await event.message.answer("В любой момент можно вернуться:", attachments=[get_back_kb()])
    except:
        await status_msg.message.edit(text="Ошибка генерации ответа.")


# --- ХЕНДЛЕРЫ ---

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    target_id = str(event.chat_id)
    await send_welcome(event, target_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    body = event.message.body
    text = (body if isinstance(body, str) else str(getattr(body, "text", ""))).strip()
    if not text or text == "None": return

    chat_id = str(event.message.recipient.chat_id)
    user_id = str(event.message.sender.user_id if event.message.sender else chat_id)
    state = assistant_client.get_state(user_id)
    if state == "DEFAULT" and user_id != chat_id:
        chat_state = assistant_client.get_state(chat_id)
        if chat_state != "DEFAULT":
            state = chat_state
            assistant_client.set_state(user_id, chat_state)

    if text.lower() in ["/start", "старт", "начать"]:
        await send_welcome(event, chat_id)
        return

    if text.lower() == "назад":
        if state == "CHATTING": await send_project_menu(event, user_id)
        else: await send_welcome(event, chat_id)
        return

    if state == "SEARCH_CENTER":
        matches = search_institutions_by_locality(text)
        SEARCH_RESULTS_CACHE[user_id] = {"query": text, "results": matches}
        await send_centers_results_page(event, user_id, page=0)
        return

    if state == "CHATTING":
        try: await bot.send_chat_action(chat_id=chat_id, action="typing")
        except: pass
        asyncio.create_task(handle_ai_stream(event, user_id, text, assistant_client.get_project(user_id)))
        return

    if state == "DEFAULT":
        await send_welcome(event, chat_id)


@dp.message_callback()
async def on_callback(event: MessageCallback):
    payload = (event.callback.payload or "").strip().lower()
    user_id = str(event.callback.user.user_id)
    state = assistant_client.get_state(user_id)

    if payload == CALLBACK_BACK:
        if state in ["CHATTING", "SEARCH_CENTER", "SELECT_PROJECT"]:
            if state == "CHATTING": await send_project_menu(event, user_id)
            else: await show_main_menu_inline(event, user_id)
        else: await show_main_menu_inline(event, user_id)
        return

    if payload == CALLBACK_ASK: await send_project_menu(event, user_id)
    elif payload == CALLBACK_EVENTS: await send_events_list(event, user_id)
    elif payload == CALLBACK_CENTERS_SEARCH: await send_centers_search_prompt(event, user_id)
    elif payload.startswith(CALLBACK_CENTERS_PAGE_PREFIX):
        page = int(payload.split(":")[-1])
        await send_centers_results_page(event, user_id, page=page)
    elif payload in {CALLBACK_GALLERY, CALLBACK_PORTFOLIO, CALLBACK_GRANT_CHECK}:
        await event.answer()
        await event.message.edit(text="Раздел в разработке 🛠", attachments=[get_back_kb()])
    elif payload in PROJECT_PAYLOAD_MAP:
        title, intent = PROJECT_PAYLOAD_MAP[payload]
        assistant_client.set_project(user_id, intent)
        assistant_client.set_state(user_id, "CHATTING")
        await event.answer()
        await event.message.edit(text=f"Выбрано: «{title}». Жду вопрос!", attachments=[get_back_kb()])


async def main():
    logger.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())