from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str

    API_URL: str = "https://api-poweron.toe.com.ua/api/a_gpv_g"
    CITY_ID: int = 21005
    DEFAULT_GROUP: str = "3.2"

    DATABASE_URL: str = "sqlite+aiosqlite:///./poweron_bot.db"

    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
