from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Configuração central; segredos vêm somente do ambiente."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    mongodb_uri: str = ""
    mongodb_db: str = "multi_agent_poc"
    # Cérebro em database PRÓPRIO, não em "ai_brain": este cluster é compartilhado com a
    # PoV singleagent, que também tem um `ai_brain` com `guardrail_policies` e `model_config`
    # de schema DIFERENTE. Com o nome genérico, o seed de uma PoV sobrescrevia a outra e o
    # /api/chat quebrava com KeyError no meio de uma demo. Descoberto exatamente assim.
    mongodb_brain_db: str = "multiagent_brain"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    grove_api_key: str = ""
    grove_anthropic_base_url: str = ""
    # Full Chat Completions URL supplied by Grove; do not infer paths from model names.
    grove_chat_completions_url: str = ""
    grove_openai_models: list[str] = []
    llm_prices: dict[str, dict[str, float]] = {}
    llm_blended_prices: dict[str, float] = {}

    @field_validator("llm_blended_prices")
    @classmethod
    def valid_blended_prices(cls, prices):
        import math
        if any(not math.isfinite(value) or value < 0 for value in prices.values()):
            raise ValueError("Médias por milhão devem ser finitas e não negativas")
        return prices

    @field_validator("llm_prices")
    @classmethod
    def valid_prices(cls, prices):
        import math
        allowed = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
        for rates in prices.values():
            if set(rates) - allowed or any(not math.isfinite(v) or v < 0 for v in rates.values()):
                raise ValueError("Tarifas devem ser finitas, não negativas e usar campos de tokens conhecidos")
        return prices
    jwt_secret: str = "desenvolvimento-inseguro-troque-este-segredo"
    jwt_ttl_minutes: int = 60
    auth_required: bool = True
    demo_token_issuance_enabled: bool = True
    admin_api_key: str = "admin-demo"
    global_turn_token_budget: int = 20000
    turn_deadline_seconds: int = 120
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    log_json: bool = False
    demo_mode: bool = False
    cors_origins: str = "http://127.0.0.1:5191"
    api_host: str = "127.0.0.1"
    api_port: int = 8031
    app_name: str = "multi-agent-poc"
    environment: str = Field(default="development", alias="ENVIRONMENT")

    # Calibrado contra o índice real (voyage-4, quantização escalar): texto idêntico só chega a ~0.84 de
    # score, não 1.0 — um threshold de 0.85+ nunca bateria nem no caso trivial. Não-relacionado mede ~0.64,
    # então a folga real é ~0.20, não os 0.15/0.07 que os números "de catálogo" 0.85/0.93 sugeriam.
    # Banda medida neste índice (voyage-4 autoEmbed + quantização escalar), 18/08/2026:
    #   texto idêntico ......... 0.8101 – 0.9213  (frase curta fica na faixa baixa)
    #   pergunta não-relacionada 0.6453 – 0.7545
    # O corte antigo do cache (0.83) ficava ACIMA do menor idêntico, então repetir uma
    # pergunta curta numa conversa nova dava MISS — justamente o beat de cache da demo.
    # Estes valores ficam no meio da folga real; o match exato por question_norm cobre
    # o resto. Recalibrar com backend/calibrate_thresholds.py se o índice mudar.
    short_term_cache_threshold: float = 0.78
    global_cache_threshold: float = 0.80
    long_term_memory_limit: int = 5
    # Warmup automático do cache semântico (ao subir e quando a UI abre); 0 desliga. Menor que o TTL do cache.
    warmup_on_start: bool = True
    warmup_cooldown_minutes: int = 45
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def use_memory_store(self) -> bool:
        return self.demo_mode or not self.mongodb_uri

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
