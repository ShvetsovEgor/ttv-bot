import json
import logging
from pathlib import Path
from typing import Dict, Any, Generator
import openai

from config import Settings
from database import AppDB

logger = logging.getLogger(__name__)

REQUIRED_RAG_INTENTS = {"BP", "GRANTS", "FORUMS", "TVOYHOD", "DP"}
INDEX_REFUSAL_PATTERNS = (
    "без доступа к актуальным данным",
    "без доступа к данным",
    "без доступа к информации",
    "не могу предоставить",
    "не могу предоставить календарные сроки",
    "не могу предоставить сроки",
    "к сожалению, без доступа",
    "не могу получить данные через инструмент",
    "without access to",
)
class PromptAgentClient:
    def __init__(self, cfg: Settings, db: AppDB):
        self.client = openai.OpenAI(
            api_key=cfg.yc_api_key,
            base_url=cfg.yc_base_url,
            project=cfg.yc_project_id,
        )
        self.project_id = cfg.yc_project_id
        self.db = db

        self.vs_map = {
            "FORUMS": cfg.vs_forums,
            "TVOYHOD": cfg.vs_tvoyhod,
            "BP": cfg.vs_bp,
            "DP": cfg.vs_dp,
            "GRANTS": cfg.vs_grants
        }

        self.prompts = {
            "TVOYHOD": "Ты — федеральный эксперт проекта «Твой Ход»...",
            "BP": "Ты — наставник конкурса «Большая Перемена»...",
            "DP": "Ты — координатор «Движения Первых»...",
            "GRANTS": "Ты — эксперт Росмолодежь.Гранты.",
            "FORUMS": "Ты — специалист по форумам Росмолодежи.",
            "GENERAL": "Ты — ИИ-навигатор по проектам Татарстана."
        }

        self.memory_path = Path("bot_memory.json")
        self.memory = self._load_memory()

    def _build_search_tool(self, intent: str) -> list[dict[str, Any]] | None:
        """
        Возвращает описание инструмента file_search для vector store.
        """
        index_id = (self.vs_map.get(intent) or "").strip()
        if not index_id:
            return None

        return [
            {
                "type": "file_search",
                "vector_store_ids": [index_id],
            }
        ]

    @staticmethod
    def _sanitize_index_refusal(text: str) -> str:
        text = (text or "").strip()
        lowered = text.lower()
        if any(pattern in lowered for pattern in INDEX_REFUSAL_PATTERNS):
            return (
                "Уточните, пожалуйста, сезон/год конкурса и этап (регистрация, полуфинал, финал), "
                "чтобы я дал точные сроки по индексу."
            )
        return text

    def _load_memory(self) -> Dict[str, Any]:
        if not self.memory_path.exists():
            return {"conversations": {}, "states": {}, "projects": {}}
        try:
            data = json.loads(self.memory_path.read_text(encoding="utf-8"))
            if "projects" not in data: data["projects"] = {}
            return data
        except Exception:
            return {"conversations": {}, "states": {}, "projects": {}}

    def _save_memory(self):
        self.memory_path.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_state(self, user_id: str, state: str):
        self.memory["states"][user_id] = state
        self._save_memory()

    def get_state(self, user_id: str) -> str:
        return self.memory["states"].get(user_id, "DEFAULT")

    def set_project(self, user_id: str, project: str):
        self.memory["projects"][user_id] = project
        self._save_memory()

    def get_project(self, user_id: str) -> str:
        return self.memory["projects"].get(user_id, "GENERAL")

    def reset_user(self, user_id: str):
        self.memory["conversations"].pop(user_id, None)
        self.memory["states"].pop(user_id, None)
        self.memory["projects"].pop(user_id, None)
        self._save_memory()

    def _classify_intent(self, text: str) -> str:
        try:
            res = self.client.chat.completions.create(
                model=f"gpt://{self.project_id}/yandexgpt-lite",
                messages=[{"role": "system",
                           "content": "Определи проект: TVOYHOD, BP, DP, GRANTS, FORUMS или GENERAL. Одно слово."},
                          {"role": "user", "content": text}],
                max_tokens=5,
                temperature=0.0
            )
            return res.choices[0].message.content.strip().upper()
        except Exception:
            return "GENERAL"

    def ask_stream(self, user_id: str, text: str, explicit_intent: str = None) -> Generator[str, None, None]:
        intent = explicit_intent if explicit_intent else self._classify_intent(text)
        self.db.log_interaction(user_id, "AI_QUERY_STREAM", intent)

        system_instruction = self.prompts.get(intent, self.prompts["GENERAL"])
        if intent in REQUIRED_RAG_INTENTS:
            system_instruction += (
                "\nЕсли вопрос связан с конкурсом/проектом, обязательно ищи ответ в подключенном индексе."
                "\nТы уже имеешь доступ к индексу через инструмент file_search."
                "\nНикогда не пиши, что у тебя нет доступа к индексу, файлам или данным."
            )

        search_tools = self._build_search_tool(intent)
        request_kwargs: dict[str, Any] = {
            "model": f"gpt://{self.project_id}/yandexgpt-lite",
            "instructions": system_instruction,
            "input": text,
        }

        if search_tools:
            index_id = (self.vs_map.get(intent) or "").strip()
            request_kwargs["tools"] = search_tools
            self.db.log_interaction(user_id, "AI_RAG_STATUS", f"RAG_ON:{intent}")
            logger.info(
                "RAG_STATUS user=%s intent=%s status=RAG_ON index=%s",
                user_id,
                intent,
                index_id,
            )
        else:
            self.db.log_interaction(user_id, "AI_RAG_STATUS", f"RAG_OFF:{intent}")
            logger.info("RAG_STATUS user=%s intent=%s status=RAG_OFF", user_id, intent)

        try:
            response = self.client.responses.create(**request_kwargs)
        except Exception as e:
            if search_tools:
                logger.warning(
                    "Не удалось включить file_search для intent=%s: %s. Повторяем запрос без RAG.",
                    intent,
                    e,
                )
                self.db.log_interaction(user_id, "AI_RAG_STATUS", f"RAG_FALLBACK:{intent}")
                logger.info("RAG_STATUS user=%s intent=%s status=RAG_FALLBACK", user_id, intent)
                request_kwargs.pop("tools", None)
                response = self.client.responses.create(**request_kwargs)
            else:
                raise

        content = self._sanitize_index_refusal((getattr(response, "output_text", None) or "").strip())
        if content:
            yield content
        else:
            yield "Не удалось получить текст ответа. Попробуйте уточнить запрос."

        if search_tools:
            # Для responses API явный маркер tool-call может не возвращаться,
            # поэтому фиксируем неопределенный статус использования.
            self.db.log_interaction(user_id, "AI_RAG_STATUS", f"RAG_USAGE_UNKNOWN:{intent}")
            logger.info("RAG_STATUS user=%s intent=%s status=RAG_USAGE_UNKNOWN", user_id, intent)