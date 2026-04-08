from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Antiduplic"
    app_env: str = "development"
    database_url: str = "sqlite:///./antiduplic.db"
    secret_key: str = "change-me"
    seed_demo_data: bool = False
    session_https_only: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    gunicorn_workers: int = 2
    gunicorn_timeout: int = 60

    initial_admin_username: str = "admin"
    initial_admin_full_name: str = "Administrador Antiduplic"
    initial_admin_email: str = "admin@example.com"
    initial_admin_password: str = Field(default="change-me-now", min_length=8)
    initial_admin_timezone: str = "America/Caracas"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.app_env.lower() != "production":
            return self

        if self.secret_key in {"", "change-me", "dev-change-me"}:
            raise ValueError("SECRET_KEY debe definirse con un valor seguro en producción.")

        if self.seed_demo_data and self.initial_admin_password in {"", "change-me-now", "admin123"}:
            raise ValueError("INITIAL_ADMIN_PASSWORD debe ser seguro si SEED_DEMO_DATA está activo en producción.")

        return self


settings = Settings()
