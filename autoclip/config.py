import os
import platform
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    series: str = "自动识别"
    style: str = "auto"
    clips: int = Field(default=2, ge=0, le=10)
    target_seconds: int = Field(default=100, ge=45, le=240)
    min_seconds: float | None = Field(default=None, ge=30)
    max_seconds: float | None = Field(default=None, le=600)
    opening: Literal["narration", "original"] = "narration"
    original_ratio_target: float = Field(default=.4, ge=.2, le=.8)
    portrait: bool = True
    vision: bool = True
    asr_backend: str = "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster"
    asr_model: str = "mlx-community/whisper-large-v3-turbo"
    asr_prompt: str = "以下是中文影视剧对白，使用简体中文。"
    chunk_seconds: int = 300
    voice: str = "zh-CN-YunxiNeural"
    tts: str = "edge"
    tts_rate: str = "+8%"
    original_volume: float = 0.035
    output: Path = Path("output")
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    max_api_calls: int = 100
    repair_attempts: int = Field(default=3, ge=0, le=5)

    @model_validator(mode="after")
    def duration_range(self):
        if self.min_seconds is not None and self.min_seconds > self.target_seconds:
            raise ValueError("min_seconds不能超过target_seconds")
        if self.max_seconds is not None and self.max_seconds < self.target_seconds:
            raise ValueError("max_seconds不能低于target_seconds")
        return self

    @classmethod
    def load(cls, path: Path | None = None):
        import tomllib
        load_dotenv()
        data = tomllib.loads(path.read_text()) if path else {}
        data.setdefault("llm_model", os.getenv("LLM_MODEL", "deepseek-flash"))
        data.setdefault("llm_base_url", os.getenv("LLM_BASE_URL", "https://api.deepseek.com"))
        return cls(**data)

    def api_key(self):
        from urllib.parse import urlparse
        host = urlparse(self.llm_base_url).hostname
        if host == "api.deepseek.com":
            key = os.getenv("DEEPSEEK_API_KEY")
            if not key and os.getenv("DEEPSEEK_USE_DASHSCOPE_ALIAS", "").lower() == "true":
                key = os.getenv("DASHSCOPE_API_KEY")
        elif host == "opencode.ai":
            key = os.getenv("OPENCODE_API_KEY")
        else:
            key = os.getenv("LLM_API_KEY")
        if not key:
            raise ValueError("缺少此服务商的 API key；请检查 .env，参阅 .env.example。")
        return key
