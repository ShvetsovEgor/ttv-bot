import asyncio
import logging
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
CALLBACK_EVENTS = "menu:events"  # НОВЫЙ РАЗДЕЛ
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
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Назад", payload=CALLBACK_BACK))
    return kb.as_markup()


# --- ФУНКЦИИ ОТПРАВКИ СОБЫТИЙ ---

async def send_events_list(event, target_id):
    """Описание мероприятий на текущую неделю"""
    text = (
        "📅 **События этой недели (27 апреля – 3 мая)**\n\n"
        "🔹 **27.04 (Пн)** — Старт регистрации на форум «ШУМ». Если хочешь в медиа — не проспи!\n"
        "🔹 **28.04 (Вт)** — Вебинар: «Как оформить смету гранта и не поседеть». В 18:00 в канале Росмолодежи.\n"
        "🔹 **30.04 (Чт)** — Дедлайн подачи заявок в трек «Делаю» проекта **Твой Ход**. Успей загрузить паспорт проекта!\n"
        "🔹 **01.05 (Пт)** — Маевка в экстрим-парке «УРАМ» (Казань). Контесты, музыка и чилл.\n"
        "🔹 **02.05 (Сб)** — Нетворкинг-встреча резидентов Иннополиса. Обсуждаем стартапы и AI-тренды.\n\n"
        "📍 Подробности и ссылки на регистрацию можно найти в официальных сообществах Министерства по делам молодежи РТ."
    )

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text=text, attachments=[get_back_kb()], format=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=str(target_id), text=text, attachments=[get_back_kb()],
                               format=ParseMode.MARKDOWN)


async def send_welcome(event, target_id):
    target_id_str = str(target_id)
    assistant_client.reset_user(target_id_str)
    assistant_client.set_state(target_id_str, "DEFAULT")

    text = (
        "Привет! 👋 \nЯ — твой ИИ-помощник по возможностям для молодёжи в Татарстане. 🧭 \n\n"
        "Выбери нужный раздел в меню ниже.\n\n"
        "\n_Продолжая пользоваться ботом, ты принимаешь [Согласие на обработку персональных данных](https://clck.su/NeKDn)._"
    )

    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Задать вопрос", payload=CALLBACK_ASK))
    kb.row(CallbackButton(text="📅 События недели", payload=CALLBACK_EVENTS))  # НОВАЯ КНОПКА
    kb.row(CallbackButton(text="Поиск молодежных центров", payload=CALLBACK_CENTERS_SEARCH))
    kb.row(CallbackButton(text="Галерея (в разработке)", payload=CALLBACK_GALLERY))
    kb.row(CallbackButton(text="Портфолио (в разработке)", payload=CALLBACK_PORTFOLIO))
    kb.row(CallbackButton(text="Проверить грантовую заявку (в разработке)", payload=CALLBACK_GRANT_CHECK))
    markup = kb.as_markup()

    if isinstance(event, MessageCallback) and event.message is not None:
        await event.answer()
        await event.message.edit(text="👇 Главное меню:", attachments=[markup], format=ParseMode.MARKDOWN)
        return

    welcome_attachments = []
    if IMAGE_PATH.exists():
        welcome_attachments.append(InputMedia(str(IMAGE_PATH)))
    welcome_attachments.append(markup)

    try:
        await bot.send_message(chat_id=target_id_str, text=text, attachments=welcome_attachments,
                               format=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await bot.send_message(chat_id=target_id_str, text=text, attachments=[markup], format=ParseMode.MARKDOWN)


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
    target_id = str(event.chat_id)
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

    if payload == CALLBACK_EVENTS:  # ОБРАБОТКА НАЖАТИЯ
        await send_events_list(event, user_id)
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