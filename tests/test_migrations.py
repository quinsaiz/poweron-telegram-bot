import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.database.config import DatabaseSettings
from src.database.models import Base, ScheduleCache

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src.poweron import service as service_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INITIAL_REVISION = "initial_schema"
PREVIOUS_HEAD_REVISION = "f412bdd7c3e0"
HEAD_REVISION = "poweron_source_state"
MIGRATIONS_ROOT = PROJECT_ROOT / "migrations"
VERSIONS_ROOT = MIGRATIONS_ROOT / "versions"


def isolated_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("BOT_TOKEN", None)
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_alembic(
    database_url: str,
    *arguments: str,
    config_path: Path = PROJECT_ROOT / "alembic.ini",
    cwd: Path = PROJECT_ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "-c", str(config_path), *arguments],
        cwd=cwd,
        env=isolated_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )


def file_tree(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class StandardAlembicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.url = f"sqlite+aiosqlite:///{self.path}"
        self.engine = create_engine(f"sqlite:///{self.path}", poolclass=NullPool)
        self.addCleanup(self.engine.dispose)

    def upgrade(self) -> None:
        result = run_alembic(self.url, "upgrade", "head")
        self.assertEqual(result.returncode, 0, result.stderr)

    def revision(self) -> str:
        with self.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        self.assertIsInstance(revision, str)
        return revision

    def test_fresh_database_upgrades_from_base_to_head(self) -> None:
        self.upgrade()

        self.assertEqual(self.revision(), HEAD_REVISION)
        self.assertEqual(
            set(inspect(self.engine).get_table_names()),
            set(Base.metadata.tables) | {"alembic_version"},
        )

    def test_repeated_upgrade_head_is_idempotent(self) -> None:
        self.upgrade()
        self.upgrade()

        self.assertEqual(self.revision(), HEAD_REVISION)
        self.assertEqual(
            set(inspect(self.engine).get_table_names()),
            set(Base.metadata.tables) | {"alembic_version"},
        )

    def test_alembic_current_reports_head_revision(self) -> None:
        self.upgrade()

        result = run_alembic(self.url, "current")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{HEAD_REVISION} (head)", result.stdout)

    def test_migrated_schema_matches_model_metadata(self) -> None:
        self.upgrade()

        inspector = inspect(self.engine)
        for table_name, model_table in Base.metadata.tables.items():
            with self.subTest(table=table_name):
                actual_columns = {
                    column["name"]: (str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(table_name)
                }
                expected_columns = {
                    column.name: (str(column.type), column.nullable)
                    for column in model_table.columns
                }
                self.assertEqual(actual_columns, expected_columns)
                self.assertEqual(
                    inspector.get_pk_constraint(table_name)["constrained_columns"],
                    [column.name for column in model_table.primary_key.columns],
                )
                self.assertEqual(
                    {
                        (constraint["name"], tuple(constraint["column_names"]))
                        for constraint in inspector.get_unique_constraints(table_name)
                    },
                    {
                        (constraint.name, tuple(column.name for column in constraint.columns))
                        for constraint in model_table.constraints
                        if isinstance(constraint, UniqueConstraint)
                    },
                )

    def test_schedule_cache_has_only_composite_unique_constraint(self) -> None:
        self.upgrade()

        inspector = inspect(self.engine)
        self.assertEqual(
            inspector.get_unique_constraints("schedule_cache"),
            [{"name": "uq_schedule_cache_date_group", "column_names": ["date_graph", "group"]}],
        )
        self.assertEqual(inspector.get_indexes("schedule_cache"), [])
        self.assertEqual(
            inspector.get_unique_constraints("users"),
            [{"name": None, "column_names": ["chat_id"]}],
        )
        self.assertEqual(
            {column["name"] for column in inspector.get_columns("users")},
            {"id", "chat_id"},
        )

    def test_previous_head_upgrade_removes_user_group_and_preserves_chat_id(self) -> None:
        previous = run_alembic(self.url, "upgrade", PREVIOUS_HEAD_REVISION)
        self.assertEqual(previous.returncode, 0, previous.stderr)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO users (chat_id, \"group\") VALUES (7, '4.1')"))

        upgrade = run_alembic(self.url, "upgrade", "head")

        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        self.assertEqual(self.revision(), HEAD_REVISION)
        self.assertEqual(
            {column["name"] for column in inspect(self.engine).get_columns("users")},
            {"id", "chat_id"},
        )
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT chat_id FROM users")), 7)

        downgrade = run_alembic(self.url, "downgrade", PREVIOUS_HEAD_REVISION)
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        self.assertEqual(columns, {"id", "chat_id", "group"})
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text('SELECT "group" FROM users')), "3.2")

    def test_outbox_constraints_foreign_key_and_due_index(self) -> None:
        self.upgrade()

        inspector = inspect(self.engine)
        delivery_columns = {
            column["name"]: column for column in inspector.get_columns("notification_deliveries")
        }
        self.assertTrue(delivery_columns["next_attempt_at"]["nullable"])
        self.assertEqual(
            inspector.get_unique_constraints("notification_deliveries"),
            [
                {
                    "name": "uq_notification_deliveries_event_chat",
                    "column_names": ["event_id", "chat_id"],
                }
            ],
        )
        self.assertEqual(
            inspector.get_indexes("notification_deliveries"),
            [
                {
                    "name": "ix_notification_deliveries_due",
                    "column_names": ["status", "next_attempt_at"],
                    "unique": 0,
                    "dialect_options": {},
                }
            ],
        )
        self.assertEqual(
            inspector.get_foreign_keys("notification_deliveries")[0]["name"],
            "fk_notification_deliveries_event_id",
        )
        self.assertEqual(
            {
                constraint["name"]
                for constraint in inspector.get_check_constraints("notification_deliveries")
            },
            {
                "ck_notification_deliveries_attempt_count",
                "ck_notification_deliveries_status",
            },
        )

    def test_upgrade_and_downgrade_preserve_users_and_cache(self) -> None:
        initial = run_alembic(self.url, "upgrade", INITIAL_REVISION)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO users (chat_id, \"group\") VALUES (7, '4.1')"))
            connection.execute(
                text(
                    "INSERT INTO schedule_cache "
                    '(date_graph, "group", times_json, updated_at) '
                    "VALUES ('2026-09-22', '4.1', '{\"00:00\": \"0\"}', "
                    "'2026-09-22 00:00:00')"
                )
            )

        upgrade = run_alembic(self.url, "upgrade", "head")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        self.assertNotIn("schedule_state", inspect(self.engine).get_table_names())
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT chat_id FROM users")), 7)
            self.assertEqual(
                connection.scalar(text("SELECT times_json FROM schedule_cache")),
                '{"00:00": "0"}',
            )

        downgrade = run_alembic(self.url, "downgrade", INITIAL_REVISION)
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        self.assertEqual(self.revision(), INITIAL_REVISION)
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("schedule_state", tables)
        self.assertNotIn("notification_deliveries", tables)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT chat_id FROM users")), 7)
            self.assertEqual(
                connection.scalar(text("SELECT times_json FROM schedule_cache")),
                '{"00:00": "0"}',
            )

    def test_autogenerate_revision_uses_template_only_in_temporary_environment(self) -> None:
        real_migrations = file_tree(MIGRATIONS_ROOT)
        copied_root = Path(self.temp_dir.name) / "copied-environment"
        copied_migrations = copied_root / "migrations"
        copied_versions = copied_migrations / "versions"
        copied_migrations.mkdir(parents=True)
        shutil.copytree(VERSIONS_ROOT, copied_versions)
        shutil.copyfile(PROJECT_ROOT / "alembic.ini", copied_root / "alembic.ini")
        shutil.copyfile(MIGRATIONS_ROOT / "env.py", copied_migrations / "env.py")
        shutil.copyfile(
            MIGRATIONS_ROOT / "script.py.mako",
            copied_migrations / "script.py.mako",
        )
        preexisting_cache = copied_versions / "__pycache__" / "preexisting-cache.pyc"
        preexisting_cache.parent.mkdir(exist_ok=True)
        preexisting_cache.write_bytes(b"pre-existing cache sentinel")
        copied_config = copied_root / "alembic.ini"
        copied_versions_before = file_tree(copied_versions)

        upgrade = run_alembic(
            self.url,
            "upgrade",
            "head",
            config_path=copied_config,
            cwd=copied_root,
        )
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        revision = run_alembic(
            self.url,
            "revision",
            "--autogenerate",
            "-m",
            "generated schema check",
            config_path=copied_config,
            cwd=copied_root,
        )
        self.assertEqual(revision.returncode, 0, revision.stderr)

        copied_versions_after = file_tree(copied_versions)
        generated_files = copied_versions_after.keys() - copied_versions_before.keys()
        self.assertEqual(len(generated_files), 1)
        generated_path = copied_versions / generated_files.pop()
        self.assertTrue(generated_path.is_file())
        self.assertTrue(generated_path.is_relative_to(copied_versions))
        self.assertFalse((VERSIONS_ROOT / generated_path.name).exists())
        spec = importlib.util.spec_from_file_location(
            "temporary_generated_revision", generated_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIsInstance(module.revision, str)
        self.assertEqual(module.down_revision, HEAD_REVISION)
        self.assertIsNone(module.branch_labels)
        self.assertIsNone(module.depends_on)
        module.upgrade()
        module.downgrade()
        self.assertEqual(
            preexisting_cache.read_bytes(),
            b"pre-existing cache sentinel",
        )
        self.assertEqual(file_tree(MIGRATIONS_ROOT), real_migrations)


class MigrationConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.url = f"sqlite+aiosqlite:///{self.path}"

    def test_alembic_upgrade_works_without_bot_token(self) -> None:
        result = run_alembic(self.url, "upgrade", "head")

        self.assertEqual(result.returncode, 0, result.stderr)
        engine = create_engine(f"sqlite:///{self.path}", poolclass=NullPool)
        self.addCleanup(engine.dispose)
        with engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT version_num FROM alembic_version")),
                HEAD_REVISION,
            )

    def test_database_url_environment_override_is_honored(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": self.url}, clear=True):
            self.assertEqual(DatabaseSettings(_env_file=None).DATABASE_URL, self.url)

    def test_database_url_local_and_container_defaults_are_unchanged(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.database.config.os.path.exists", return_value=False),
        ):
            self.assertEqual(
                DatabaseSettings(_env_file=None).DATABASE_URL,
                "sqlite+aiosqlite:///./data/poweron_bot.db",
            )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.database.config.os.path.exists", return_value=True),
        ):
            self.assertEqual(
                DatabaseSettings(_env_file=None).DATABASE_URL,
                "sqlite+aiosqlite:////app/data/poweron_bot.db",
            )

    def test_application_configuration_requires_bot_token(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import src.config"],
            cwd=self.temp_dir.name,
            env=isolated_environment(self.url),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOT_TOKEN", result.stderr)


class EntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.bin_dir = Path(self.temp_dir.name) / "bin"
        self.bin_dir.mkdir()
        self.uvicorn_marker = Path(self.temp_dir.name) / "uvicorn-started"

    def write_executable(self, name: str, contents: str) -> None:
        path = self.bin_dir / name
        path.write_text(contents)
        path.chmod(0o755)

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin_dir}:{environment['PATH']}"
        environment["UVICORN_MARKER"] = str(self.uvicorn_marker)
        return environment

    def test_migration_failure_preserves_status_prints_safe_guidance_and_skips_uvicorn(
        self,
    ) -> None:
        self.write_executable("alembic", "#!/bin/sh\nexit 23\n")
        self.write_executable("uvicorn", '#!/bin/sh\ntouch "$UVICORN_MARKER"\n')

        environment = self.environment()
        secrets = {
            "BOT_TOKEN": "secret-bot-token",
            "DATABASE_URL": "sqlite+aiosqlite:////secret/database.db",
            "PRIVATE_VALUE": "other-secret-value",
        }
        environment.update(secrets)

        result = subprocess.run(
            ["sh", str(PROJECT_ROOT / "entrypoint.sh")],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 23)
        self.assertFalse(self.uvicorn_marker.exists())
        self.assertIn("old disposable pre-Alembic database", result.stderr)
        self.assertIn("stop the service", result.stderr)
        self.assertIn("back up or rename the database manually", result.stderr)
        self.assertIn("then retry", result.stderr)
        self.assertIn("never deletes or stamps databases automatically", result.stderr)
        combined_output = result.stdout + result.stderr
        for name, value in secrets.items():
            with self.subTest(secret=name):
                self.assertNotIn(value, combined_output)

    def test_success_runs_migration_then_execs_uvicorn_with_default_port(self) -> None:
        order = Path(self.temp_dir.name) / "order"
        self.write_executable("alembic", f'#!/bin/sh\necho "alembic $*" >> "{order}"\n')
        self.write_executable(
            "uvicorn",
            f'#!/bin/sh\necho "uvicorn $*" >> "{order}"\ntouch "$UVICORN_MARKER"\n',
        )

        result = subprocess.run(
            ["sh", str(PROJECT_ROOT / "entrypoint.sh")],
            cwd=PROJECT_ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.uvicorn_marker.exists())
        self.assertEqual(
            order.read_text().splitlines(),
            [
                "alembic upgrade head",
                "uvicorn src.main:app --host 0.0.0.0 --port 9999",
            ],
        )

    def test_configured_port_is_forwarded(self) -> None:
        arguments = Path(self.temp_dir.name) / "uvicorn-arguments"
        self.write_executable("alembic", "#!/bin/sh\nexit 0\n")
        self.write_executable("uvicorn", f'#!/bin/sh\nprintf "%s\\n" "$*" > "{arguments}"\n')
        environment = self.environment()
        environment["PORT"] = "8765"

        result = subprocess.run(
            ["sh", str(PROJECT_ROOT / "entrypoint.sh")],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            arguments.read_text().strip(),
            "src.main:app --host 0.0.0.0 --port 8765",
        )

    def test_uvicorn_replaces_entrypoint_shell_process(self) -> None:
        pid_file = Path(self.temp_dir.name) / "uvicorn-pid"
        self.write_executable("alembic", "#!/bin/sh\nexit 0\n")
        self.write_executable("uvicorn", f'#!/bin/sh\necho "$$" > "{pid_file}"\n')

        process = subprocess.Popen(
            ["sh", str(PROJECT_ROOT / "entrypoint.sh")],
            cwd=PROJECT_ROOT,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(int(pid_file.read_text()), process.pid)


class CacheWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.url = f"sqlite+aiosqlite:///{self.path}"
        migration = await asyncio.to_thread(run_alembic, self.url, "upgrade", "head")
        self.assertEqual(migration.returncode, 0, migration.stderr)
        self.engine = create_async_engine(self.url, poolclass=NullPool)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session_patch = patch.object(service_module, "async_session", self.session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def rows(self) -> list[ScheduleCache]:
        async with self.session() as session:
            return list((await session.execute(select(ScheduleCache))).scalars().all())

    async def test_two_groups_coexist_and_updates_are_isolated(self) -> None:
        save = service_module.PowerService.save_schedule_to_cache
        await save("2024-01-15", "3.2", {"00:00": "0"})
        await save("2024-01-15", "4.1", {"00:00": "1"})
        ids = {row.group: row.id for row in await self.rows()}

        await save("2024-01-15", "3.2", {"00:00": "10"})

        rows = await self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.group: json.loads(row.times_json) for row in rows},
            {"3.2": {"00:00": "10"}, "4.1": {"00:00": "1"}},
        )
        self.assertEqual({row.group: row.id for row in rows}, ids)

    async def test_concurrent_same_key_upserts_do_not_raise(self) -> None:
        save = service_module.PowerService.save_schedule_to_cache

        await asyncio.gather(
            *(save("2024-01-15", "3.2", {"00:00": str(index % 2)}) for index in range(8))
        )

        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(json.loads(rows[0].times_json), ({"00:00": "0"}, {"00:00": "1"}))
