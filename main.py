import asyncio
import json
import logging
import re
import html
from dataclasses import dataclass
from typing import Dict, Any
import os
from pathlib import Path
import openai
from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated, InputMedia, MessageButton
from maxapi.enums.parse_mode import ParseMode
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Settings:
    max_api_token: str
    yc_api_key: str
    yc_project_id: str
    yc_base_url: str

    # Индексы файлов
    vs_forums: str
    vs_tvoyhod: str
    vs_bp: str
    vs_dp: str
    vs_grants: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            max_api_token=_required_env("MAX_API_TOKEN"),
            yc_api_key=_required_env("YC_API_KEY"),
            yc_project_id=_required_env("YC_PROJECT_ID"),
            yc_base_url=os.getenv("YC_BASE_URL", "https://ai.api.cloud.yandex.net/v1"),
            vs_forums=os.getenv("YC_VS_FORUMS", ""),
            vs_tvoyhod=os.getenv("YC_VS_TVOYHOD", ""),
            vs_bp=os.getenv("YC_VS_BP", ""),
            vs_dp=os.getenv("YC_VS_DP", ""),
            vs_grants=os.getenv("YC_VS_GRANTS", ""),
        )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


class PromptAgentClient:
    def __init__(self, cfg: Settings):
        self.client = openai.OpenAI(
            api_key=cfg.yc_api_key,
            base_url=cfg.yc_base_url,
            project=cfg.yc_project_id,
        )
        self.project_id = cfg.yc_project_id

        # Маппинг категорий на ID векторных хранилищ
        self.vs_map = {
            "FORUMS": cfg.vs_forums,
            "TVOYHOD": cfg.vs_tvoyhod,
            "BP": cfg.vs_bp,
            "DP": cfg.vs_dp,
            "GRANTS": cfg.vs_grants
        }

        # Память (храним и сессии Yandex, и FSM-состояния бота)
        self.memory_path = Path("bot_memory.json")
        self.memory = self._load_memory()

    def _load_memory(self) -> Dict[str, Any]:
        if not self.memory_path.exists():
            return {"conversations": {}, "states": {}}
        try:
            return json.loads(self.memory_path.read_text(encoding="utf-8"))
        except Exception:
            return {"conversations": {}, "states": {}}

    def _save_memory(self) -> None:
        temp_path = self.memory_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.memory_path)

    # --- FSM (Управление состояниями) ---
    def set_state(self, user_id: str, state: str) -> None:
        self.memory["states"][user_id] = state
        self._save_memory()

    def get_state(self, user_id: str) -> str:
        return self.memory["states"].get(user_id, "DEFAULT")

    def reset_user(self, user_id: str) -> None:
        """Сбрасывает и диалог (сессию), и состояние"""
        if user_id in self.memory["conversations"]:
            del self.memory["conversations"][user_id]
        if user_id in self.memory["states"]:
            del self.memory["states"][user_id]
        self._save_memory()

    @staticmethod
    def _sanitize_output(text: str) -> str:
        cleaned = re.sub(r"\[search_index\s*[\s\S]*?\]", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    # --- LLM Роутер (Классификатор) ---
    def _classify_intent(self, text: str) -> str:
        """Дешевый запрос для определения нужного файла"""
        request_payload = {
            "model": f"gpt://{self.project_id}/yandexgpt-lite",
            "input": text,
            "max_output_tokens": 5,
            "temperature": 0.0,  # Ноль, чтобы не фантазировал
            "instructions": (
                "Определи категорию вопроса пользователя. Выбери строго ОДИН вариант из списка:\n"
                "FORUMS (вопросы про форумы Росмолодежи)\n"
                "TVOYHOD (вопросы про Твой Ход)\n"
                "BP (вопросы про Большую перемену)\n"
                "DP (вопросы про Движение первых)\n"
                "GRANTS (вопросы про гранты)\n"
                "GENERAL (всё остальное: приветствия, общие вопросы)\n"
                "Ответь только одним словом латиницей. Без точек."
            )
        }
        try:
            response = self.client.responses.create(**request_payload)
            intent = getattr(response, "output_text", "GENERAL").strip().upper()
            logger.info(f"LLM Роутер определил категорию: {intent}")
            return intent if intent in self.vs_map else "GENERAL"
        except Exception:
            return "GENERAL"

    # --- Основной генератор ответа ---
    def ask(self, user_id: str, text: str) -> str:
        state = self.get_state(user_id)
        conv_id = self.memory["conversations"].get(user_id)

        # 1. ОБРАБОТКА FSM-СОСТОЯНИЙ (когда пользователь внутри сценария кнопок)
        if state == "CHECK_GRANT":
            request_payload = {
                "model": f"gpt://{self.project_id}/yandexgpt",
                "input": text,
                "max_output_tokens": 2000,
                "instructions": (
                    "Ты — суровый, но справедливый эксперт Росмолодежь.Гранты. "
                    "Пользователь прислал тебе текст своей проектной заявки. "
                    "Оцени её: укажи сильные стороны, слабые места и дай 3 конкретных совета по улучшению."
                )
            }
            try:
                response = self.client.responses.create(**request_payload)
                # После проверки возвращаем человека в обычный режим
                self.set_state(user_id, "DEFAULT")
                return self._sanitize_output(
                    getattr(response, "output_text", "")) + "\n\n*(Режим проверки заявок выключен)*"
            except Exception:
                return "Не удалось проверить заявку. Попробуй позже."

        # 2. СТАНДАРТНЫЙ РЕЖИМ (Роутинг по файлам)
        if not conv_id:
            try:
                conv = self.client.conversations.create()
                conv_id = conv.id
                self.memory["conversations"][user_id] = conv_id
                self._save_memory()
            except Exception:
                return "Не удалось начать диалог."

        # Узнаем, какой файл нужен для ответа
        intent = self._classify_intent(text)
        vector_store_id = self.vs_map.get(intent)

        request_payload = {
            "model": f"gpt://{self.project_id}/yandexgpt",
            "input": text,
            "conversation": conv_id,
            "max_output_tokens": 2000,
            "instructions": (
                "Ты — ИИ-консультант по молодежным возможностям. "
                "Если к запросу подключен инструмент поиска по файлам (file_search), ОБЯЗАТЕЛЬНО ищи информацию там. "
                "Никогда не говори, что у тебя нет доступа к файлам."
            )
        }

        # Если роутер выбрал конкретную базу — прикрепляем её
        if vector_store_id:
            request_payload["tools"] = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]

        try:
            response = self.client.responses.create(**request_payload)
            return self._sanitize_output(getattr(response, "output_text", "Агент не ответил."))
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            return "Произошла ошибка при обращении к базе знаний."


settings = Settings.from_env()
bot = Bot(settings.max_api_token)
dp = Dispatcher()
assistant_client = PromptAgentClient(settings)

# Печатная машинка
TYPEWRITER_DELAY_SECONDS = 0.03
TYPEWRITER_BATCH_CHARS = 15


async def send_with_typewriter(event: MessageCreated, text: str) -> None:
    if not text:
        return

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


BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "static" / "img" / "intro.png"


@dp.message_created(Command("start"))
async def on_start(event: MessageCreated) -> None:
    user_id = str(event.message.sender.user_id if event.message.sender else event.message.recipient.chat_id)
    assistant_client.reset_user(user_id)

    welcome_text = (
        "Привет! Я — твой ИИ-навигатор по молодежной политике Татарстана и федеральным проектам. 🧭\n\n"
        "Вот что я умею:\n"
        "🔹 **Отвечать на вопросы**: просто спроси меня про «Твой Ход», «Большую перемену», «Движение Первых» или форумы Росмолодежи.\n"
        "🔹 **Проверять грантовые заявки**: нажми соответствующую кнопку в меню, отправь свой текст, и я разберу его как строгий эксперт.\n"
        "🔹 **Искать события**: используй меню для быстрого доступа к новостям и галерее мероприятий.\n\n"
        "Какой маршрут проложим сегодня?"
    )

    # Собираем красивую инлайн-клавиатуру
    builder = InlineKeyboardBuilder()
    # Делаем главную кнопку на всю ширину
    builder.row(MessageButton(text="Проверить грантовую заявку"))
    # Делаем две кнопки в один ряд
    builder.row(
        MessageButton(text="Галерея событий"),
        MessageButton(text="Агрегатор новостей")
    )
    keyboard_markup = builder.as_markup()

    if IMAGE_PATH.exists():
        # Отправляем картинку вместе с клавиатурой в списке attachments
        await event.message.answer(
            text=welcome_text,
            attachments=[InputMedia(str(IMAGE_PATH)), keyboard_markup],
            format=ParseMode.MARKDOWN
        )
    else:
        # Отправляем только текст и клавиатуру
        await event.message.answer(
            text=welcome_text,
            attachments=[keyboard_markup],
            format=ParseMode.MARKDOWN
        )

@dp.message_created()
async def on_message(event: MessageCreated) -> None:
    body = event.message.body
    text = (getattr(body, "text", body) or "").strip()
    if not text:
        return

    sender = event.message.sender
    recipient = event.message.recipient
    user_id_value = (sender.user_id if sender else None) or recipient.user_id or recipient.chat_id
    if not user_id_value:
        return
    user_id = str(user_id_value)

    # === ОБРАБОТКА КНОПОК МЕНЮ (ПЕРЕКЛЮЧЕНИЕ FSM) ===
    if text == "Проверить грантовую заявку":
        assistant_client.set_state(user_id, "CHECK_GRANT")
        await event.message.answer(
            "🕵️‍♂️ *Режим эксперта активирован!*\nОтправь мне текст твоей заявки (описание, цели, задачи), и я разберу её по косточкам.",
            format=ParseMode.MARKDOWN)
        return

    elif text == "Галерея событий":
        # Здесь не нужен AI, просто отдаем заготовленный текст или ссылку
        await event.message.answer(
            "📸 *Галерея событий*\nПосмотреть расписание ближайших форумов и мероприятий можно на [нашем портале](https://myrosmol.ru/).",
            format=ParseMode.MARKDOWN)
        return

    elif text == "Агрегатор новостей":
        await event.message.answer(
            "📰 *Свежие новости*\n- Стартовал прием заявок на 'Твой Ход'\n- Форум 'ШУМ' открыл регистрацию\nСледи за обновлениями!",
            format=ParseMode.MARKDOWN)
        return

    # === ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ ===
    try:
        await event.bot.send_chat_action(chat_id=user_id, action="typing")
    except Exception:
        pass

    response_text = assistant_client.ask(user_id=user_id, text=text)
    await send_with_typewriter(event, response_text)


async def main() -> None:
    logger.info("Бот запущен с LLM-роутером и FSM.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())