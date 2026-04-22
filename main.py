import asyncio
import json
import logging
import re
import html
from pathlib import Path

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, MessageCreated, InputMedia, MessageButton
from maxapi.enums.parse_mode import ParseMode
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

# Нативные классы для работы со вложениями
from maxapi.types.attachments.image import Image, PhotoAttachmentRequestPayload
from maxapi.types.attachments.file import File
from maxapi.types.attachments.attachment import Attachment

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

# --- АНИМАЦИЯ ПЕЧАТИ ---
TYPEWRITER_DELAY_SECONDS = 0.03
TYPEWRITER_BATCH_CHARS = 15

async def send_with_typewriter(event: MessageCreated, text: str) -> None:
    if not text: return
    final_html = html.escape(text)
    final_html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', final_html)
    final_html = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', final_html)
    final_html = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', final_html)
    typing_text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    typing_text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', typing_text)
    typing_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', typing_text)

    first_chunk = typing_text[:TYPEWRITER_BATCH_CHARS] or typing_text
    sent = await event.message.answer(first_chunk)

    if not sent:
        await event.message.answer(final_html, format=ParseMode.HTML)
        return

    message = sent.message
    rendered = first_chunk

    if len(typing_text) > TYPEWRITER_BATCH_CHARS:
        for idx, char in enumerate(typing_text[TYPEWRITER_BATCH_CHARS:], start=1):
            rendered += char
            if idx % TYPEWRITER_BATCH_CHARS == 0 or idx == len(typing_text[TYPEWRITER_BATCH_CHARS:]):
                try:
                    await message.edit(text=rendered)
                    await asyncio.sleep(TYPEWRITER_DELAY_SECONDS)
                except Exception:
                    pass

    try:
        await message.edit(text=final_html, format=ParseMode.HTML)
    except Exception:
        await message.edit(text=typing_text)


# --- ХЕНДЛЕРЫ ---
async def send_welcome(event, user_id):
    assistant_client.reset_user(user_id)
    text = (
        "Привет! Я — твой ИИ-навигатор по молодежной политике Татарстана и федеральным проектам. 🧭\n\n"
        "Вот что я умею:\n"
        "🔹 **Отвечать на вопросы**: просто спроси меня про проекты Росмолодежи.\n"
        "🔹 **Проверять грантовые заявки**: нажми кнопку ниже.\n"
        "🔹 **Галерея событий**: смотри актуальные фото.\n\n"
        "Какой маршрут проложим сегодня?\n\n"
        "\n_Продолжая работу с ботом, вы принимаете [Согласие на обработку персональных данных](https://clck.su/NeKDn)._"
    )
    kb = InlineKeyboardBuilder()
    kb.row(MessageButton(text="Проверить грантовую заявку"))
    kb.row(MessageButton(text="Галерея событий"))

    markup = kb.as_markup()
    chat_id = event.chat_id if hasattr(event, 'chat_id') else event.message.recipient.chat_id

    if IMAGE_PATH.exists():
        await event.bot.send_message(chat_id=chat_id, text=text, attachments=[InputMedia(str(IMAGE_PATH))],
                                     format=ParseMode.MARKDOWN)
        await event.bot.send_message(chat_id=chat_id, text="👇 Выбери действие в меню:", attachments=[markup],
                                     format=ParseMode.MARKDOWN)
    else:
        await event.bot.send_message(chat_id=chat_id, text=text, attachments=[markup], format=ParseMode.MARKDOWN)

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

    if text == "None":
        text = ""
    text = text.strip()

    sender = event.message.sender
    user_id = str(sender.user_id if sender else event.message.recipient.chat_id)
    db.upsert_user(user_id, sender)
    state = assistant_client.get_state(user_id)

    att_to_save = None
    file_id = None

    attachments = getattr(event.message.body, "attachments", [])

    if attachments:
        att = attachments[0]
        att_to_save = att.model_dump_json()

        file_id = getattr(att, "file_id", None) or getattr(att, "id", None)
        if not file_id and hasattr(att, "payload"):
            payload = att.payload
            if isinstance(payload, dict):
                file_id = payload.get("photo_id") or payload.get("file_id") or payload.get("id")
            else:
                file_id = getattr(payload, "photo_id", None) or getattr(payload, "file_id", None) or getattr(payload, "id", None)

    has_photo = bool(file_id)

    # 1. ОБРАБОТКА ГАЛЕРЕИ (АДМИНКА)
    if state == "WAITING_FOR_PHOTO":
        if has_photo:
            caption = text if text else "Событие АНО ТТВ"
            db.add_gallery_post(file_id=att_to_save, caption=caption)
            assistant_client.set_state(user_id, "DEFAULT")
            await event.message.answer("✅ Фото успешно добавлено в Галерею событий!")
            return

        if text.lower() == "отмена":
            assistant_client.set_state(user_id, "DEFAULT")
            await event.message.answer("Действие отменено.")
            return

        await event.message.answer("❌ Я не вижу фотографии или файла. Пожалуйста, отправь картинку (или напиши 'отмена').")
        return

    if has_photo and state != "WAITING_FOR_PHOTO":
        await event.message.answer("💡 *Подсказка:*\nЕсли ты хочешь добавить это фото в Галерею событий, сначала отправь мне команду `/addphoto`, а затем пришли фото.", format=ParseMode.MARKDOWN)
        return

    # 2. АДМИН-КОМАНДЫ
    if text.startswith("/addphoto"):
        assistant_client.set_state(user_id, "WAITING_FOR_PHOTO")
        await event.message.answer("📸 Отправь мне фотографию с мероприятия.\nТекст сообщения будет использован как подпись.")
        return

    if not text: return

    if text.lower() in ["/start", "старт", "начать"]:
        await send_welcome(event, user_id)
        return

    # 3. КНОПКИ МЕНЮ
    if text == "Проверить грантовую заявку":
        assistant_client.set_state(user_id, "CHECK_GRANT")
        db.log_interaction(user_id, "MENU_BUTTON", "CHECK_GRANT")
        await event.message.answer("🕵️‍♂️ *Режим эксперта активирован!*\nОтправь мне текст твоей заявки (описание, цели, задачи), и я разберу её по косточкам.", format=ParseMode.MARKDOWN)
        return

    elif text == "Галерея событий":
        db.log_interaction(user_id, "MENU_BUTTON", "GALLERY")
        posts = db.get_latest_gallery_posts(limit=3)

        if not posts:
            await event.message.answer("📸 Галерея пока пуста. Администраторы скоро добавят новые фото!")
            return

        await event.message.answer("📸 *Последние события ТТВ:*", format=ParseMode.MARKDOWN)

        for post in posts:
            try:
                data = json.loads(post.file_id)

                if not isinstance(data, dict):
                    continue

                payload_in = data.get("payload", {})
                att_type = data.get("type", "image")

                photo_id = payload_in.get("photo_id") or payload_in.get("file_id") or payload_in.get("id")
                url = payload_in.get("url")
                token = payload_in.get("token")

                if att_type == "image":
                    req_payload = PhotoAttachmentRequestPayload(
                        photos=str(photo_id) if photo_id else None,
                        url=url,
                        token=token
                    )
                    attachment = Attachment.model_construct(type="image", payload=req_payload)
                else:
                    attachment = Attachment.model_construct(type=att_type, payload=payload_in)

                await event.message.answer(text=post.caption, attachments=[attachment])

            except Exception as e:
                logger.error(f"Пропущена сломанная запись в галерее: {e}")
                continue

        return

    # 4. ЗАПРОС К ИИ
    status = await event.message.answer("⏳ *Изучаю запрос...*", format=ParseMode.MARKDOWN)

    try:
        await event.bot.send_chat_action(chat_id=user_id, action="typing")
    except Exception:
        pass

    response = await asyncio.to_thread(assistant_client.ask, user_id, text)

    if status.message:
        try:
            await status.message.delete()
        except Exception:
            pass

    await send_with_typewriter(event, response)


async def main():
    logger.info("Бот запущен. Модульная архитектура активирована.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())