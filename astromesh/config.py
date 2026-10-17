from functools import lru_cache
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASTROMESH_", env_file=".env", extra="ignore")

    llm_provider: str = "crusoe"
    llm_base_url: str = "https://api.inference.crusoecloud.com/v1"
    # Use the model ID returned by this account's authenticated Crusoe catalog.
    llm_model: str = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
    llm_api_key: str = ""
    crusoe_api_key: str = Field(default="", validation_alias="CRUSOE_API_KEY")
    nvidia_api_key: str = Field(default="", validation_alias="NVIDIA_API_KEY")
    runtime_mode: str = "local"
    tick_interval_sec: float = 0.5
    random_seed: int = 17
    minimum_debris_distance_km: float = 5.0

    @property
    def inference_api_key(self) -> str:
        if self.llm_provider.lower() == "crusoe":
            # Provider is explicitly configured as Crusoe. Prefer its dedicated
            # secret, while supporting the existing provider-neutral setting.
            return (self.crusoe_api_key or os.getenv("CRUSOE_API_KEY", "") or
                    self.llm_api_key or os.getenv("ASTROMESH_LLM_API_KEY", ""))
        return self.nvidia_api_key or os.getenv("NVIDIA_API_KEY", "") or self.llm_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
