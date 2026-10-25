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
    duration_profile: Literal["short", "long"] = "short"
    narrative_mode: Literal["analysis", "story_first"] = "story_first"
    target_seconds: int = Field(default=100, ge=45, le=1200)
    min_seconds: float | None = Field(default=None, ge=30)
    max_seconds: float | None = Field(default=None, le=3600)
    opening: Literal["narration", "original", "original_teaser"] = "narration"
    original_ratio_target: float = Field(default=.4, ge=.2, le=.8)
    portrait: bool = True
    vision: bool = True
    research_before_produce: bool = False
    oral_edit: bool = False
    identity_gate: bool = False
    identity_registry: Path | None = None
    asr_backend: str = "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster"
    asr_model: str = "mlx-community/whisper-large-v3-turbo"
    asr_prompt: str = "以下是中文影视剧对白，使用简体中文。"
    chunk_seconds: int = 300
    voice: str = "zh-CN-YunyangNeural"
    tts: Literal["edge", "azure", "macos"] = "edge"
    edge_endpoint: Literal["bing", "msedge"] = "msedge"
    macos_voice: str = "Tingting"
    macos_rate: int = Field(default=240, ge=120, le=450)
    azure_speech_region: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")
    tts_rate: str = "+8%"
    original_volume: float = 0.035
    output: Path = Path("output")
    # Runtime locations are excluded from legacy plan/review fingerprints.
    continuity_history: Path | None = Field(default=None, exclude=True)
    previous_episodes: int = Field(default=3, ge=0, exclude=True)
    episode_order: dict[str, int] = Field(default_factory=dict, exclude=True)
    media_cache: Path | None = Field(default=None, exclude=True)
    conflict_brief: bool = Field(default=False, exclude=True)
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    max_api_calls: int = Field(default=100, ge=1)
    # Request scheduling is operational, not an editorial/source input. Changing
    # it must not discard transcripts, drafts, voices or existing approvals.
    api_budget_mode: Literal['fixed', 'steps'] = Field(default='fixed', exclude=True)
    repair_attempts: int = Field(default=3, ge=0, le=5)

    def api_call_policy(self):
        return {'mode':self.api_budget_mode,
                'global_request_limit':self.max_api_calls if self.api_budget_mode=='fixed' else None,
                'transport_attempts_per_request':3,
                'repair_retries_per_step':self.repair_attempts}

    @model_validator(mode="after")
    def duration_range(self):
        if self.opening == "original_teaser" and self.duration_profile != "long":
            raise ValueError("高光前置目前用于长片提纲与定稿渲染，需duration_profile=long")
        if self.duration_profile == "short" and (self.target_seconds > 240 or (self.max_seconds or 0) > 600):
            raise ValueError("加长解说需显式设置duration_profile=long；短片配置不自动放宽")
        if self.duration_profile == "long" and self.target_seconds < 300:
            raise ValueError("加长解说target_seconds至少300秒")
        if self.identity_gate and not self.vision:
            raise ValueError("人物归属检查需要vision=true")
        if self.min_seconds is not None and self.min_seconds > self.target_seconds:
            raise ValueError("min_seconds不能超过target_seconds")
        if self.max_seconds is not None and self.max_seconds < self.target_seconds:
            raise ValueError("max_seconds不能低于target_seconds")
        return self

    @classmethod
    def load(cls, path: Path | None = None):
        import tomllib
        load_dotenv(path.resolve().parent / ".env" if path else None)
        data = tomllib.loads(path.read_text()) if path else {}
        if path:
            base = path.resolve().parent
            for name, default in (("continuity_history", "output"), ("media_cache", "output/.media-cache")):
                location = Path(data.get(name, default)).expanduser()
                data[name] = (base / location).resolve()
        registry = os.getenv("AUTOCLIP_IDENTITY_REGISTRY") or data.get("identity_registry")
        if registry:
            registry = Path(registry).expanduser()
            data["identity_registry"] = (path.resolve().parent / registry if path and not registry.is_absolute() else registry).resolve()
        data.setdefault("azure_speech_region", os.getenv("AZURE_SPEECH_REGION") or None)
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
