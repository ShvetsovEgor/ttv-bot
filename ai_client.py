import json
import logging
import re
from pathlib import Path
from typing import Dict, Any
import openai

from config import Settings
from database import AppDB

logger = logging.getLogger(__name__)


class PromptAgentClient:
    def __init__(self, cfg: Settings, db: AppDB):
        self.client = openai.OpenAI(
            api_key=cfg.yc_api_key,
            base_url=cfg.yc_base_url,
            project=cfg.yc_project_id,
        )
        self.project_id = cfg.yc_project_id
        self.db = db

        # Маппинг баз знаний берем из конфига
        self.vs_map = {
            "FORUMS": cfg.vs_forums,
            "TVOYHOD": cfg.vs_tvoyhod,
            "BP": cfg.vs_bp,
            "DP": cfg.vs_dp,
            "GRANTS": cfg.vs_grants
        }

        self.prompts = {
            "TVOYHOD": (
                "Ты — федеральный эксперт и наставник всероссийского студенческого проекта «Твой Ход». "
                "Твоя задача — консультировать студентов по трекам проекта (например, «Делаю», «Первопроходец»), "
                "правилам участия, формированию портфолио и получению премии в 1 миллион рублей. "
                "Отвечай мотивирующе, энергично и по существу. Используй молодежный, но профессиональный стиль общения. "
                "Опирайся на официальные правила проекта."
            ),
            "BP": (
                "Ты — дружелюбный наставник всероссийского конкурса «Большая Перемена». "
                "Помогай школьникам (5-10 классы) и студентам колледжей (СПО) разбираться в вызовах конкурса, "
                "этапах прохождения (от знакомства до финала), решении кейсов и командной работе. "
                "Поддерживай их, вселяй уверенность и объясняй сложные правила конкурса простым и доступным языком. "
                "Твой тон — эмпатичный, поддерживающий и воодушевляющий."
            ),
            "DP": (
                "Ты — энергичный координатор Общероссийского общественно-государственного движения детей и молодежи «Движение Первых». "
                "Твоя цель — рассказывать детям, их родителям и наставникам о ценностях движения, "
                "флагманских проектах, акциях и возможностях для саморазвития. "
                "Будь позитивным, инклюзивным и всегда подчеркивай важность созидательного труда и любви к Родине."
            ),
            "GRANTS": (
                "Ты — строгий, компетентный и объективный федеральный эксперт конкурса «Росмолодежь.Гранты». "
                "Консультируй пользователей по правилам подачи заявок, формированию сметы, описанию социальной значимости, "
                "календарному плану и критериям оценки (актуальность, масштабность, публичность и т.д.). "
                "Отвечай профессионально, четко, структурно и без лишней воды. Давай конструктивные советы по улучшению проектов."
            ),
            "FORUMS": (
                "Ты — специалист по форумной кампании Росмолодежи. Помогай пользователям ориентироваться в линейке "
                "всероссийских и окружных форумов (например, «ШУМ», «Территория смыслов», «Таврида», «Машук», «ОстроVа» и др.). "
                "Подробно объясняй процесс регистрации через ФГАИС «Молодежь России», требования к участникам, "
                "написание мотивационных писем и этапы отбора. Будь полезным и точным навигатором."
            ),
            "GENERAL": (
                "Ты — дружелюбный ИИ-навигатор по молодежной политике Республики Татарстан и проектам АНО «Татарстан — территория возможностей» (ТТВ). "
                "Отвечай на общие вопросы вежливо, позитивно и кратко. Помогай пользователям найти нужную информацию о "
                "молодежных событиях в Татарстане и России. Если запрос не относится к конкретному проекту, давай универсальный, но полезный ответ."
            )
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
        prompt = (
            "Определи проект: TVOYHOD, BP, DP, GRANTS, FORUMS или GENERAL. "
            "Ответь только одним словом."
        )
        try:
            res = self.client.responses.create(
                model=f"gpt://{self.project_id}/yandexgpt-lite",
                input=text,
                instructions=prompt,
                max_output_tokens=5,
                temperature=0.0
            )
            intent = getattr(res, "output_text", "GENERAL").strip().upper()
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
                instructions="Ты федеральный эксперт Росмолодежь.Гранты. Оцени заявку."
            )
            self.set_state(user_id, "DEFAULT")
            return getattr(res, "output_text", "") + "\n\n*(Режим эксперта выключен)*"

        if not conv_id:
            conv = self.client.conversations.create()
            conv_id = conv.id
            self.memory["conversations"][user_id] = conv_id
            self._save_memory()

        intent = self._classify_intent(text)
        self.db.log_interaction(user_id, "AI_QUERY", intent)

        system_instruction = self.prompts.get(intent, self.prompts["GENERAL"])
        vs_id = self.vs_map.get(intent)

        payload = {
            "model": f"gpt://{self.project_id}/yandexgpt-lite",
            "input": text,
            "conversation": conv_id,
            "instructions": system_instruction
        }

        if vs_id:
            payload["tools"] = [{"type": "file_search", "vector_store_ids": [vs_id]}]

        res = self.client.responses.create(**payload)
        answer = getattr(res, "output_text", "").strip()
        answer = re.sub(r"\[search_index.*?\]", "", answer, flags=re.I).strip()

        intent_names = {
            "TVOYHOD": "Твой Ход", "BP": "Большая Перемена",
            "DP": "Движение Первых", "GRANTS": "Росмолодежь.Гранты",
            "FORUMS": "Форумная кампания"
        }

        if intent in intent_names:
            return f"🎯 _Контекст: {intent_names[intent]}_\n\n{answer}"
        return answer