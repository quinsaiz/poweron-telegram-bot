import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator

from src.database.config import DatabaseSettings

SUPPORTED_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


def _validate_ipv6_hostname(hostname: str, raw_authority: str) -> None:
    if not raw_authority.startswith("["):
        raise ValueError("POWERON_API_URL IPv6 hosts must be bracketed")
    closing_bracket = raw_authority.find("]")
    suffix = raw_authority[closing_bracket + 1 :]
    if closing_bracket < 0 or (suffix and (not suffix.startswith(":") or not suffix[1:].isdigit())):
        raise ValueError("POWERON_API_URL must include a valid bracketed IPv6 host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise ValueError("POWERON_API_URL must include a valid IPv6 host") from None
    if not isinstance(address, ipaddress.IPv6Address):
        raise ValueError("POWERON_API_URL must include a valid IPv6 host")


def _validate_dns_or_ipv4_hostname(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv4Address):
        return
    if all(character in "0123456789." for character in hostname):
        raise ValueError("POWERON_API_URL must include a valid IPv4 host")
    if len(hostname) > 253:
        raise ValueError("POWERON_API_URL hostname is too long")

    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or not DNS_LABEL_PATTERN.fullmatch(label)
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise ValueError("POWERON_API_URL must include a valid ASCII DNS hostname")


def _validate_api_hostname(hostname: str, raw_authority: str) -> None:
    if "%" in raw_authority:
        raise ValueError("POWERON_API_URL host must not be percent-encoded")
    if ":" in hostname:
        _validate_ipv6_hostname(hostname, raw_authority)
    else:
        _validate_dns_or_ipv4_hostname(hostname)


class Settings(DatabaseSettings):
    BOT_TOKEN: SecretStr = Field(min_length=1)
    POWERON_CITY_ID: int = Field(gt=0)
    POWERON_API_URL: str

    LOG_LEVEL: str = "INFO"

    @field_validator("BOT_TOKEN", mode="before")
    @classmethod
    def validate_bot_token(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("BOT_TOKEN must not be empty")
        return value

    @field_validator("POWERON_API_URL")
    @classmethod
    def validate_poweron_api_url(cls, value: str) -> str:
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError("POWERON_API_URL must not include whitespace or control characters")
        if "\\" in value:
            raise ValueError("POWERON_API_URL must not include backslashes")
        if "?" in value or "#" in value:
            raise ValueError("POWERON_API_URL must not include a query or fragment delimiter")

        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise ValueError("POWERON_API_URL must use HTTPS")
        if not parsed.hostname:
            raise ValueError("POWERON_API_URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("POWERON_API_URL must not include credentials")
        _validate_api_hostname(parsed.hostname, parsed.netloc)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("POWERON_API_URL must include a valid optional port") from None
        if parsed.netloc.endswith(":") or port == 0:
            raise ValueError("POWERON_API_URL must include a valid optional port")
        if parsed.path not in {"/api", "/api/"}:
            raise ValueError("POWERON_API_URL path must be exactly /api")
        return urlunsplit((parsed.scheme, parsed.netloc, "/api", "", ""))

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in SUPPORTED_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(SUPPORTED_LOG_LEVELS)}")
        return normalized


settings = Settings()
