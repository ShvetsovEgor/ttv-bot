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

# --- ИМПОРТЫ ИЗ ВАШИХ МОДУЛЕЙ ---
from config import Settings
from database import AppDB
from ai_client import PromptAgentClient

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "static" / "img" / "intro.png"

# --- ИНИЦИАЛИЗАЦИЯ ---
settings = Settings.from_env()
bot = Bot(settings.max_api_token)
dp = Dispatcher()
db = AppDB()
assistant_client = PromptAgentClient(settings, db)

# Константы колбэков
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


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_back_kb():
    """Клавиатура с кнопкой Назад"""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


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


def _clean_field_value(value: str | None, fallback: str = "Не указано") -> str:
    if not value: return fallback
    cleaned = str(value).replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    return cleaned or fallback


def search_institutions_by_locality(locality: str, max_results: int = 200) -> list[dict]:
    query = locality.strip()
    if not query or not YOUTH_POLICY_DB_PATH.exists(): return []

    with sqlite3.connect(YOUTH_POLICY_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = conn.execute("SELECT name, address_phone, head_name, web_resources FROM institutions").fetchall()

    normalized_query = _normalize_for_match(query)
    if not normalized_query: return []
    query_variants = _expand_locality_variants(normalized_query)

    direct_matches = []
    for row in all_rows:
        combined = _normalize_for_match(f"{row['name']} {row['address_phone']}")
        if any(variant in combined for variant in query_variants):
            direct_matches.append(dict(row))

    if direct_matches: return direct_matches[:max_results]

    ranked = []
    for row in all_rows:
        name = _normalize_for_match(row["name"] or "")
        score = max(SequenceMatcher(None, variant, name).ratio() for variant in query_variants)
        if score >= 0.55: ranked.append((score, dict(row)))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in ranked[:max_results]]


def format_institutions_response(locality: str, rows: list[dict], total_count: int, page: int) -> str:
    if not rows: return f"По запросу «{locality}» ничего не нашлось."
    start_idx = page * CENTERS_PAGE_SIZE + 1
    end_idx = min(total_count, (page + 1) * CENTERS_PAGE_SIZE)
    header = f"Организации по запросу «{locality}»:\n(Найдено: {total_count}, показаны {start_idx}-{end_idx})\n"
    parts = [header]
    for idx, row in enumerate(rows, start=1):
        parts.append(f"{idx}) {row.get('name')}\n   Адрес: {_clean_field_value(row.get('address_phone'))}")
    return "\n\n".join(parts)


def get_centers_page_kb(page: int, total_count: int):
    kb = InlineKeyboardBuilder()
    total_pages = (total_count + CENTERS_PAGE_SIZE - 1) // CENTERS_PAGE_SIZE
    nav = []
    if page > 0: nav.append(CallbackButton(text="⬅️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page - 1}"))
    if page < total_pages - 1: nav.append(
        CallbackButton(text="➡️", payload=f"{CALLBACK_CENTERS_PAGE_PREFIX}{page + 1}"))
    if nav: kb.row(*nav)
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


# --- ОСНОВНЫЕ ФУНКЦИИ ОТПРАВКИ ---

async def send_welcome(event, target_id):
    """Отправляет приветственное сообщение, картинку и главное меню в ОДНОМ сообщении"""
    target_id_str = str(target_id)
    assistant_client.reset_user(target_id_str)
    assistant_client.set_state(target_id_str, "DEFAULT")
    URL = "https://xn-----6kcca1a0clhajkadginefbh2i.xn--p1ai/%D0%A1%D0%BE%D0%B3%D0%BB%D0%B0%D1%81%D0%B8%D0%B5%20%D0%BD%D0%B0%20%D0%9E%D0%9F%D0%94.pdf"
    text = (
        "Привет! 👋\n"
"Я — твой ИИ-помощник по возможностям для молодёжи в Татарстане и России. 🧭\n" 
"Выбери нужный раздел в меню ниже.\n\n"
f"_Продолжая пользоваться ботом, ты принимаешь [Соглашение на обработку персональных данных]({URL})._"
    )

    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Задать вопрос", payload=CALLBACK_ASK))
    kb.row(CallbackButton(text="Поиск молодежных центров", payload=CALLBACK_CENTERS_SEARCH))
    kb.row(CallbackButton(text="Галерея (в разработке)", payload=CALLBACK_GALLERY))
    kb.row(CallbackButton(text="Портфолио (в разработке)", payload=CALLBACK_PORTFOLIO))
    kb.row(CallbackButton(text="Проверить грантовую заявку (в разработке)", payload=CALLBACK_GRANT_CHECK))
    markup = kb.as_markup()

    # Редактирование (для MessageCallback, например, кнопка "Назад")
    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        # При редактировании картинку менять нельзя, редактируем только текст и кнопки
        await event.message.edit(text="👇 Главное меню:", attachments=[markup], format=ParseMode.MARKDOWN)
        return

    # --- ИЗМЕНЕНИЕ: СОЕДИНЯЕМ КАРТИНКУ ---
    # Подготавливаем список вложений для одного сообщения
    welcome_attachments = []
    image_attachment = None

    # 1. Если файл картинки существует, создаем InputMedia и добавляем
    if IMAGE_PATH.exists():
        image_attachment = InputMedia(str(IMAGE_PATH))
        welcome_attachments.append(image_attachment)

    # 2. Всегда добавляем клавиатуру во вложения
    welcome_attachments.append(markup)

    # 3. Отправляем ОДНО сообщение
    try:
        # Пробуем отправить всё вместе
        await bot.send_message(
            chat_id=target_id_str,
            text=text,
            attachments=welcome_attachments,
            format=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Ошибка объединенной отправки: {e}. Пробуем отправить без картинки.")
        # FALLBACK: Если отправка с картинкой упала, пробуем отправить только текст и кнопки.
        try:
            # Если картинка была в списке, удаляем её
            if image_attachment and image_attachment in welcome_attachments:
                welcome_attachments.remove(image_attachment)

            # Пробуем еще раз (с текстом и кнопками)
            await bot.send_message(
                chat_id=target_id_str,
                text=text,
                attachments=welcome_attachments,
                format=ParseMode.MARKDOWN
            )
        except Exception as e2:
            logger.critical(f"Критическая ошибка fallback отправки приветствия: {e2}")


async def send_project_menu(event, user_id):
    assistant_client.set_state(str(user_id), "SELECT_PROJECT")
    text = "Выбери проект, по которому хочешь задать вопрос:"
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Большая перемена", payload="project:bp"),
           CallbackButton(text="Твой Ход", payload="project:tvoyhod"))
    kb.row(CallbackButton(text="Форумы", payload="project:forums"),
           CallbackButton(text="Гранты", payload="project:grants"))
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[kb.as_markup()])
    else:
        await event.message.answer(text, attachments=[kb.as_markup()])


async def handle_ai_stream(event, user_id, text, intent):
    status_msg = await event.message.answer("⏳ *Изучаю запрос...*", format=ParseMode.MARKDOWN)
    partial_text = ""
    try:
        for chunk in assistant_client.ask_stream(str(user_id), text, explicit_intent=intent):
            partial_text = chunk
        await status_msg.message.edit(text=partial_text, format=ParseMode.MARKDOWN)
        await event.message.answer("Спроси еще или вернись назад:", attachments=[get_back_kb()])
    except:
        await status_msg.message.edit(text="Ошибка генерации.")


# --- ХЕНДЛЕРЫ ---

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    # СОГЛАСНО ДОКУМЕНТАЦИИ: используем chat_id
    target_id = str(event.chat_id)
    logger.info(f"⚡️ Бот запущен в чате {target_id}")
    await send_welcome(event, target_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    body = event.message.body
    text = body if isinstance(body, str) else str(getattr(body, "text", ""))
    if not text or text == "None": return
    text = text.strip()

    chat_id = str(event.message.recipient.chat_id)
    user_id = str(event.message.sender.user_id if event.message.sender else chat_id)
    state = assistant_client.get_state(user_id)

    if text.lower() in ["/start", "старт", "начать"]:
        await send_welcome(event, chat_id)
        return

    if text.lower() == "назад":
        if state == "CHATTING":
            await send_project_menu(event, user_id)
        else:
            await send_welcome(event, chat_id)
        return

    if state == "CHATTING":
        asyncio.create_task(handle_ai_stream(event, user_id, text, assistant_client.get_project(user_id)))
        return

    if state == "DEFAULT":
        await send_welcome(event, chat_id)


@dp.message_callback()
async def on_callback(event: MessageCallback):
    payload = (event.callback.payload or "").lower()
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
        assistant_client.set_state(user_id, "SEARCH_CENTER")
        await event.message.edit(text="Введите город для поиска центров:", attachments=[get_back_kb()])
        return

    if payload in PROJECT_PAYLOAD_MAP:
        title, intent = PROJECT_PAYLOAD_MAP[payload]
        assistant_client.set_project(user_id, intent)
        assistant_client.set_state(user_id, "CHATTING")
        await event.answer()
        await event.message.edit(text=f"Вы выбрали «{title}». Слушаю твой вопрос!", attachments=[get_back_kb()])


async def main():
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())