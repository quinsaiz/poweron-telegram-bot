import logging
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "123:test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src.config import Settings, settings
    from src.database.config import DatabaseSettings
    from src.poweron.groups import PowerOnGroupResolver
    from src.poweron.service import PowerService


class SettingsTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "BOT_TOKEN": "123:test-only-token",
            "POWERON_CITY_ID": 21005,
            "POWERON_API_URL": "https://api-poweron.toe.com.ua/api/",
            "LOG_LEVEL": "INFO",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def test_valid_settings_are_normalized_without_revealing_token(self) -> None:
        configured = self.settings(LOG_LEVEL="warning")

        self.assertEqual(configured.POWERON_API_URL, "https://api-poweron.toe.com.ua/api")
        self.assertEqual(configured.LOG_LEVEL, "WARNING")
        self.assertNotIn("123:test-only-token", repr(configured))

    def test_city_must_be_positive(self) -> None:
        for city_id in (0, -1):
            with self.subTest(city_id=city_id), self.assertRaises(ValidationError):
                self.settings(POWERON_CITY_ID=city_id)

    def test_api_url_accepts_only_canonical_api_roots(self) -> None:
        accepted = (
            ("https://example.test/api", "https://example.test/api"),
            ("https://example.test/api/", "https://example.test/api"),
            ("https://sub.example.test/api", "https://sub.example.test/api"),
            ("https://127.0.0.1/api", "https://127.0.0.1/api"),
            ("https://[2001:db8::1]/api", "https://[2001:db8::1]/api"),
            ("https://example.test:8443/api", "https://example.test:8443/api"),
            ("HTTPS://example.test/api", "https://example.test/api"),
        )
        for value, expected in accepted:
            with self.subTest(value=value):
                self.assertEqual(self.settings(POWERON_API_URL=value).POWERON_API_URL, expected)

    def test_api_url_rejects_noncanonical_or_unsafe_values(self) -> None:
        invalid_urls = (
            "http://api-poweron.toe.com.ua/api",
            "https:///api",
            "https://user:pass@api-poweron.toe.com.ua/api",
            "https://api-poweron.toe.com.ua/api?x=1",
            "https://api-poweron.toe.com.ua/api#fragment",
            "https://example.test/api?",
            "https://example.test/api#",
            "https://example.test/api?key=",
            "https://example.test/api/? ",
            "https://api-poweron.toe.com.ua",
            "https://api-poweron.toe.com.ua/",
            "https://api-poweron.toe.com.ua/api/api",
            "https://api-poweron.toe.com.ua/api/a_gpv_g",
            "https://api-poweron.toe.com.ua/api/a_gpv_g/extra",
            "https://api-poweron.toe.com.ua/other",
            "https://api-poweron.toe.com.ua:/api",
            "https://api-poweron.toe.com.ua:invalid/api",
            "https://api-poweron.toe.com.ua:0/api",
            "https://api-poweron.toe.com.ua:99999/api",
            "https://example .test/api",
            "https://example%20.test/api",
            "https://example_test/api",
            "https://-example.test/api",
            "https://example-.test/api",
            "https://example..test/api",
            "https://999.1.1.1/api",
            "https://example.test/api\\extra",
            " https://example.test/api",
            "https://example.test/api ",
            "https://example.test/\tapi",
            "https://example.test/\napi",
        )
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.settings(POWERON_API_URL=url)

    def test_canonical_root_constructs_exact_endpoints(self) -> None:
        with patch.object(settings, "POWERON_API_URL", "https://custom.test:8443/api"):
            self.assertEqual(
                PowerService().schedule_url,
                "https://custom.test:8443/api/a_gpv_g",
            )
            self.assertEqual(
                PowerOnGroupResolver()._group_url,
                "https://custom.test:8443/api/pw-accounts/building-groups",
            )

    def test_empty_token_and_unknown_log_level_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.settings(BOT_TOKEN="   ")
        with self.assertRaises(ValidationError):
            self.settings(LOG_LEVEL="verbose")

    def test_validation_errors_hide_sensitive_input_values(self) -> None:
        cases = (
            (
                "URL credentials",
                lambda: self.settings(
                    POWERON_API_URL="https://fake-user:URL_PASSWORD_NEVER_PRINT@example.test/api"
                ),
                "URL_PASSWORD_NEVER_PRINT",
                "POWERON_API_URL",
            ),
            (
                "URL query",
                lambda: self.settings(
                    POWERON_API_URL="https://example.test/api?token=QUERY_SECRET_NEVER_PRINT"
                ),
                "QUERY_SECRET_NEVER_PRINT",
                "POWERON_API_URL",
            ),
            (
                "bot token",
                lambda: self.settings(BOT_TOKEN={"secret": "BOT_SECRET_NEVER_PRINT"}),
                "BOT_SECRET_NEVER_PRINT",
                "BOT_TOKEN",
            ),
            (
                "database URL",
                lambda: DatabaseSettings(
                    _env_file=None,
                    DATABASE_URL={"password": "DATABASE_SECRET_NEVER_PRINT"},
                ),
                "DATABASE_SECRET_NEVER_PRINT",
                "DATABASE_URL",
            ),
        )
        for name, build, secret, field_name in cases:
            with self.subTest(name=name):
                with self.assertRaises(ValidationError) as caught:
                    build()
                rendered = str(caught.exception)
                self.assertNotIn(secret, rendered)
                self.assertIn("validation error", rendered)
                self.assertIn(field_name, rendered)

    def test_logged_validation_error_does_not_reveal_url_secret(self) -> None:
        secret = "STARTUP_URL_SECRET_NEVER_PRINT"
        try:
            self.settings(
                POWERON_API_URL=f"https://user:{secret}@example.test/api",
            )
        except ValidationError as error:
            with self.assertLogs("settings-startup-test", level="ERROR") as logs:
                logging.getLogger("settings-startup-test").exception(
                    "Startup settings validation failed",
                    exc_info=error,
                )
        else:
            self.fail("invalid URL must fail validation")

        self.assertNotIn(secret, "\n".join(logs.output))
