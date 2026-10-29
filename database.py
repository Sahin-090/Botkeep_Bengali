import asyncio
import json
import logging
import socket
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from logging_setup import log_event

log = logging.getLogger("database")
SCHEMA_PATH = Path(__file__).parent / "database" / "schema.sql"


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class Database:
    def __init__(self, dsn: str, min_size: int, max_size: int) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max(max_size, min_size)
        self.pool: asyncpg.Pool | None = None

    def _target(self) -> str:
        """host:port/db without the password - safe to show in logs and errors."""
        try:
            u = urlsplit(self._dsn)
            return f"{u.hostname}:{u.port or 5432}{u.path}"
        except ValueError:
            return "<unparsable DATABASE_URL>"

    async def _create_pool(self) -> asyncpg.Pool:
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                return await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min,
                    max_size=self._max,
                    command_timeout=30,
                    timeout=20,
                    init=_init_connection,
                )
            except socket.gaierror as exc:      # host name does not exist / DNS failure
                raise RuntimeError(
                    f"DATABASE_URL host cannot be resolved ({self._target()}). Check the host name "
                    "in your DATABASE_URL: it must be a PUBLIC PostgreSQL host (Neon, Supabase, "
                    "Aiven ...). Hosts like *.railway.internal or 'localhost' only work inside "
                    "their own platform."
                ) from exc
            except (OSError, asyncio.TimeoutError, asyncpg.CannotConnectNowError,
                    asyncpg.ConnectionDoesNotExistError) as exc:   # transient: retry
                last = exc
                log.warning("database_connect_retry attempt=%s target=%s error=%s",
                            attempt, self._target(), exc)
                await asyncio.sleep(2 * attempt)
            except asyncpg.InvalidPasswordError as exc:
                raise RuntimeError("DATABASE_URL has a wrong user name or password.") from exc
            except asyncpg.InvalidCatalogNameError as exc:
                raise RuntimeError(f"Database in DATABASE_URL does not exist ({self._target()}).") from exc
        raise RuntimeError(f"Cannot connect to PostgreSQL at {self._target()}: {last}") from last

    async def connect(self) -> None:
        self.pool = await self._create_pool()
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock(7000)")
            try:
                await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            finally:
                await conn.execute("SELECT pg_advisory_unlock(7000)")
        log_event(log, logging.INFO, "database_ready")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
