"""Database settings shared by the bot and migration commands."""

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_database_url() -> str:
    return (
        "sqlite+aiosqlite:////app/data/poweron_bot.db"
        if os.path.exists("/app/data")
        else "sqlite+aiosqlite:///./data/poweron_bot.db"
    )


class DatabaseSettings(BaseSettings):
    DATABASE_URL: str = Field(default_factory=default_database_url)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


database_settings = DatabaseSettings()
