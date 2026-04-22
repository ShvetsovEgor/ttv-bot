import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

@dataclass
class Settings:
    max_api_token: str
    yc_api_key: str
    yc_project_id: str
    yc_base_url: str
    db_url: str
    # ID векторных хранилищ (RAG)
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
            # Если DATABASE_URL не задан, используем sqlite по умолчанию
            db_url=os.getenv("DATABASE_URL", "sqlite:///app_data.db"),
            vs_forums=os.getenv("YC_VS_FORUMS", ""),
            vs_tvoyhod=os.getenv("YC_VS_TVOYHOD", ""),
            vs_bp=os.getenv("YC_VS_BP", ""),
            vs_dp=os.getenv("YC_VS_DP", ""),
            vs_grants=os.getenv("YC_VS_GRANTS", ""),
        )