from src.database.config import DatabaseSettings


class Settings(DatabaseSettings):
    BOT_TOKEN: str

    API_URL: str = "https://api-poweron.toe.com.ua/api/a_gpv_g"
    CITY_ID: int = 21005
    DEFAULT_GROUP: str = "3.2"

    LOG_LEVEL: str = "INFO"


settings = Settings()
