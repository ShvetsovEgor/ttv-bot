import asyncio
import json
import logging
import re
import html
import os
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any

import openai
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated, InputMedia, MessageButton
from maxapi.enums.parse_mode import ParseMode
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

# Загрузка переменных окружения
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "static" / "img" / "intro.png"


# --- НАСТРОЙКИ ---
@dataclass
class Settings:
    max_api_token: str
    yc_api_key: str
    yc_project_id: str
    yc_base_url: str

    vs_forums: str
    vs_tvoyhod: str
    vs_bp: str
    vs_dp: str
    vs_grants: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            max_api_token=os.getenv("MAX_API_TOKEN"),
            yc_api_key=os.getenv("YC_API_KEY"),
            yc_project_id=os.getenv("YC_PROJECT_ID"),
            yc_base_url=os.getenv("YC_BASE_URL", "https://ai.api.cloud.yandex.net/v1"),
            vs_forums=os.getenv("YC_VS_FORUMS", ""),
            vs_tvoyhod=os.getenv("YC_VS_TVOYHOD", ""),
            vs_bp=os.getenv("YC_VS_BP", ""),
            vs_dp=os.getenv("YC_VS_DP", ""),
            vs_grants=os.getenv("YC_VS_GRANTS", ""),
        )


# --- БАЗА ДАННЫХ (SQLAlchemy) ---
Base = declarative_base()


class UserModel(Base):
    __tablename__ = 'users'
    user_id = Column(String, primary_key=True)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    first_seen = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InteractionModel(Base):
    __tablename__ = 'interactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    action_type = Column(String)
    action_detail = Column(String)
    timestamp = Column(DateTime, default=datetime.now)


class AnalyticsDB:
    def __init__(self, db_url="sqlite:///analytics.db"):
        self.engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def upsert_user(self, user_id: str, sender_obj: Any):
        if not sender_obj: return
        with self.Session() as session:
            user = session.query(UserModel).filter_by(user_id=user_id).first()
            if not user:
                user = UserModel(
                    user_id=user_id,
                    username=getattr(sender_obj, "username", ""),
                    first_name=getattr(sender_obj, "first_name", ""),
                    last_name=getattr(sender_obj, "last_name", "")
                )
                session.add(user)
            else:
                user.username = getattr(sender_obj, "username", "")
                user.first_name = getattr(sender_obj, "first_name", "")
                user.last_name = getattr(sender_obj, "last_name", "")
            session.commit()

    def log_interaction(self, user_id: str, action_type: str, action_detail: str = ""):
        with self.Session() as session:
            session.add(InteractionModel(
                user_id=user_id,
                action_type=action_type,
                action_detail=action_detail
            ))
            session.commit()


# --- ИИ КЛИЕНТ ---
class PromptAgentClient:
    def __init__(self, cfg: Settings):
        self.client = openai.OpenAI(
            api_key=cfg.yc_api_key,
            base_url=cfg.yc_base_url,
            project=cfg.yc_project_id,
        )
        self.project_id = cfg.yc_project_id
        self.vs_map = {
            "FORUMS": cfg.vs_forums, "TVOYHOD": cfg.vs_tvoyhod,
            "BP": cfg.vs_bp, "DP": cfg.vs_dp, "GRANTS": cfg.vs_grants
        }
        self.memory_path = Path("bot_memory.json")
        self.memory = self._load_memory()

    def _load_memory(self) -> Dict[str, Any]:
        if not self.memory_path.exists(): return {"conversations": {}, "states": {}}
        try:
            return json.loads(self.memory_path.read_text(encoding="utf-8"))
        except:
            return {"conversations": {}, "states": {}}

    def _save_memory(self):
        self.memory_path.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_state(self, user_id: str, state: str):
        self.memory["states"][user_id] = state
        self._save_memory()

    def get_state(self, user_id: str) -> str:
        return self.memory["states"].get(user_id, "DEFAULT")

    def reset_user(self, user_id: str):
        self.memory["conversations"].pop(user_id, None)
        self.memory["states"].pop(user_id, None)
        self._save_memory()

    def _classify_intent(self, text: str) -> str:
        try:
            response = self.client.responses.create(
                model=f"gpt://{self.project_id}/yandexgpt-lite",
                input=text,
                max_output_tokens=5,
                temperature=0.0,
                instructions="Классифицируй запрос: FORUMS, TVOYHOD, BP, DP, GRANTS или GENERAL. Ответь одним словом."
            )
            intent = getattr(response, "output_text", "GENERAL").strip().upper()
            return re.sub(r'[^A-Z]', '', intent)
        except:
            return "GENERAL"

    def ask(self, user_id: str, text: str) -> str:
        state = self.get_state(user_id)
        conv_id = self.memory["conversations"].get(user_id)

        if state == "CHECK_GRANT":
            res = self.client.responses.create(
                model=f"gpt://{self.project_id}/yandexgpt",
                input=text,
                instructions="Ты эксперт Росмолодежь.Гранты. Оцени заявку: сильные стороны, слабые стороны и 3 совета."
            )
            self.set_state(user_id, "DEFAULT")
            return getattr(res, "output_text", "") + "\n\n*(Режим эксперта выключен)*"

        if not conv_id:
            conv = self.client.conversations.create()
            conv_id = conv.id
            self.memory["conversations"][user_id] = conv_id
            self._save_memory()

        intent = self._classify_intent(text)
        analytics_db.log_interaction(user_id, "AI_QUERY", intent)

        vs_id = self.vs_map.get(intent)
        payload = {
            "model": f"gpt://{self.project_id}/yandexgpt-lite",
            "input": text,
            "conversation": conv_id,
            "instructions": "Ты ИИ-навигатор. Используй поиск по файлам, если он доступен."
        }
        if vs_id: payload["tools"] = [{"type": "file_search", "vector_store_ids": [vs_id]}]

        res = self.client.responses.create(**payload)
        return re.sub(r"\[search_index.*?\]", "", getattr(res, "output_text", ""), flags=re.I).strip()


# --- ИНИЦИАЛИЗАЦИЯ ---
settings = Settings.from_env()
bot = Bot(settings.max_api_token)
dp = Dispatcher()
analytics_db = AnalyticsDB()
assistant_client = PromptAgentClient(settings)

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
        "🔹 **Отвечать на вопросы**: просто спроси меня про «Твой Ход», «Большую перемену», «Движение Первых» или форумы Росмолодежи.\n"
        "🔹 **Проверять грантовые заявки**: нажми кнопку ниже, отправь свой текст, и я разберу его как эксперт.\n"
        "🔹 **Искать события**: используй меню для быстрого доступа к новостям.\n\n"
        "Какой маршрут проложим сегодня?\n\n"
        "\nПродолжая работу с ботом, вы принимаете [Согласие на обработку персональных данных](https://clck.su/NeKDn)."
    )
    kb = InlineKeyboardBuilder()
    kb.row(MessageButton(text="Проверить грантовую заявку"))
    kb.row(MessageButton(text="Галерея событий"), MessageButton(text="Агрегатор новостей"))

    markup = kb.as_markup()
    chat_id = event.chat_id if hasattr(event, 'chat_id') else event.message.recipient.chat_id

    if IMAGE_PATH.exists():
        await event.bot.send_message(chat_id=chat_id, text=text, attachments=[InputMedia(str(IMAGE_PATH)), markup],
                                     format=ParseMode.MARKDOWN)
    else:
        await event.bot.send_message(chat_id=chat_id, text=text, attachments=[markup], format=ParseMode.MARKDOWN)


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = str(event.user_id or event.chat_id)
    analytics_db.log_interaction(user_id, "COMMAND", "BotStarted")
    await send_welcome(event, user_id)


@dp.message_created(Command("start"))
async def on_start(event: MessageCreated):
    user_id = str(event.message.sender.user_id if event.message.sender else event.message.recipient.chat_id)
    analytics_db.upsert_user(user_id, event.message.sender)
    analytics_db.log_interaction(user_id, "COMMAND", "/start")
    await send_welcome(event, user_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    text = (getattr(event.message.body, "text", event.message.body) or "").strip()
    if not text: return

    sender = event.message.sender
    user_id = str(sender.user_id if sender else event.message.recipient.chat_id)
    analytics_db.upsert_user(user_id, sender)

    if text.lower() in ["/start", "старт", "начать"]:
        await send_welcome(event, user_id)
        return

    # --- КНОПКИ МЕНЮ ---
    if text == "Проверить грантовую заявку":
        assistant_client.set_state(user_id, "CHECK_GRANT")
        analytics_db.log_interaction(user_id, "MENU_BUTTON", "CHECK_GRANT")
        await event.message.answer(
            "🕵️‍♂️ *Режим эксперта активирован!*\nОтправь мне текст твоей заявки (описание, цели, задачи), и я разберу её по косточкам.",
            format=ParseMode.MARKDOWN)
        return

    elif text == "Галерея событий":
        analytics_db.log_interaction(user_id, "MENU_BUTTON", "GALLERY")
        await event.message.answer(
            "📸 *Галерея событий*\nПосмотреть расписание ближайших форумов можно на [нашем портале](https://myrosmol.ru/).",
            format=ParseMode.MARKDOWN)
        return

    elif text == "Агрегатор новостей":
        analytics_db.log_interaction(user_id, "MENU_BUTTON", "NEWS")
        await event.message.answer(
            "📰 *Свежие новости*\n- Стартовал прием заявок на 'Твой Ход'\n- Форум 'ШУМ' открыл регистрацию\nСледи за обновлениями!",
            format=ParseMode.MARKDOWN)
        return
    # -------------------

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
    logger.info("Бот запущен. База данных аналитики активна.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())