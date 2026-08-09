from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    rime_api_key: str = ""
    rime_api_url: str = "https://users.rime.ai/v1"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    whisper_model_path: str = "models/ggml-base.en.bin"

    # Supabase Auth. When both are set, credentials are stored/verified
    # against Supabase Auth; otherwise a local dev fallback (data/users.json)
    # is used so the app can still run without external services.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Secret used to sign the local-dev fallback tokens.
    auth_secret: str = "voxvault-local-dev-secret"

    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
