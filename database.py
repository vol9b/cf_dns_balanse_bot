import os
import time
import logging
import aiosqlite
from typing import List, Set, Tuple, Optional


async def db_connect(db_path: str) -> aiosqlite.Connection:
    """Подключение к базе данных с обработкой ошибок"""
    try:
        # Создаем директорию для БД если не существует
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logging.info(f"Создана директория для БД: {db_dir}")

        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row

        # Настройки для производительности и надежности
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA cache_size=10000")
        await conn.execute("PRAGMA temp_store=MEMORY")

        logging.info(f"Подключение к БД установлено: {db_path}")
        return conn

    except Exception as e:
        logging.error(f"Ошибка подключения к БД {db_path}: {e}")
        raise


async def db_init(conn: aiosqlite.Connection) -> None:
    """Инициализация схемы базы данных с обработкой ошибок"""
    try:
        # Создание таблицы dns_records
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dns_records (
                id TEXT PRIMARY KEY,
                zone_id TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                ttl INTEGER NOT NULL,
                proxied INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_checked_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )

        # Создание индексов для производительности
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dns_records_name_type ON dns_records(name, type);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dns_records_zone_id ON dns_records(zone_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dns_records_status ON dns_records(status);")

        # Создание таблицы host_states
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS host_states (
                zone_id TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                last_status TEXT NOT NULL,
                last_checked_at INTEGER,
                last_changed_at INTEGER,
                -- anti-flap fields
                consec_up INTEGER NOT NULL DEFAULT 0,
                consec_down INTEGER NOT NULL DEFAULT 0,
                stable_status TEXT NOT NULL DEFAULT 'unknown',
                stable_changed_at INTEGER,
                PRIMARY KEY (zone_id, name, type, content)
            );
            """
        )

        # Создание индексов для host_states
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_host_states_name ON host_states(name);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_host_states_stable_status ON host_states(stable_status);")

        # Миграции для совместимости со старыми схемами
        async with conn.execute("PRAGMA table_info(host_states)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]

        async def ensure_col(name: str, ddl: str) -> None:
            if name not in cols:
                logging.info(f"Добавляем колонку {name} в таблицу host_states")
                await conn.execute(f"ALTER TABLE host_states ADD COLUMN {ddl}")

        await ensure_col("consec_up", "consec_up INTEGER NOT NULL DEFAULT 0")
        await ensure_col("consec_down", "consec_down INTEGER NOT NULL DEFAULT 0")
        await ensure_col("stable_status", "stable_status TEXT NOT NULL DEFAULT 'unknown'")
        await ensure_col("stable_changed_at", "stable_changed_at INTEGER")
        # SLA tracking columns (monthly)
        await ensure_col("sla_month", "sla_month TEXT")  # YYYY-MM format
        await ensure_col("sla_up_seconds", "sla_up_seconds INTEGER NOT NULL DEFAULT 0")
        await ensure_col("sla_down_seconds", "sla_down_seconds INTEGER NOT NULL DEFAULT 0")
        await ensure_col("sla_last_update", "sla_last_update INTEGER")

        # Таблица настроек доменов (вкл/выкл балансировки)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_settings (
                zone_id TEXT NOT NULL,
                hostname TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                added_at INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (zone_id, hostname)
            );
            """
        )

        # Миграция: добавляем колонку deleted если её нет
        async with conn.execute("PRAGMA table_info(domain_settings)") as cursor:
            ds_cols = [r[1] for r in await cursor.fetchall()]
        if "deleted" not in ds_cols:
            await conn.execute("ALTER TABLE domain_settings ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")

        await conn.commit()
        logging.info("Схема базы данных инициализирована успешно")

    except Exception as e:
        logging.error(f"Ошибка инициализации БД: {e}")
        await conn.rollback()
        raise


async def db_upsert_record(conn: aiosqlite.Connection, rec: dict, status: str = "unknown", ts_val: Optional[int] = None) -> None:
    now = int(time.time())
    await conn.execute(
        """
        INSERT INTO dns_records (id, zone_id, name, type, content, ttl, proxied, status, last_checked_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            zone_id=excluded.zone_id,
            name=excluded.name,
            type=excluded.type,
            content=excluded.content,
            ttl=excluded.ttl,
            proxied=excluded.proxied,
            updated_at=excluded.updated_at
        ;
        """,
        (
            rec["id"],
            rec["zone_id"],
            rec["name"],
            rec["type"],
            rec["content"],
            int(rec.get("ttl", 1) or 1),
            1 if rec.get("proxied") else 0,
            status,
            ts_val,
            now,
            now,
        ),
    )


async def db_update_status(conn: aiosqlite.Connection, record_id: str, status: str) -> None:
    now = int(time.time())
    await conn.execute(
        "UPDATE dns_records SET status=?, last_checked_at=?, updated_at=? WHERE id=?",
        (status, now, now, record_id),
    )


async def row_changed_at(conn: aiosqlite.Connection, zone_id: str, name: str, type_: str, content: str) -> int:
    async with conn.execute(
        "SELECT last_changed_at FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["last_changed_at"]) if row and row["last_changed_at"] is not None else int(time.time())


def _current_month() -> str:
    """Get current month in YYYY-MM format."""
    return time.strftime("%Y-%m")


async def db_upsert_host_state(
    conn: aiosqlite.Connection, zone_id: str, name: str, type_: str,
    content: str, status: str, up_threshold: int, down_threshold: int
) -> Tuple[str, str, str, str]:
    """Update aggregated state per (zone_id, name, type, content).
    Returns (prev_last_status, new_last_status, prev_stable_status, new_stable_status).
    """
    now = int(time.time())
    current_month = _current_month()

    async with conn.execute(
        "SELECT last_status, consec_up, consec_down, stable_status, "
        "sla_month, sla_up_seconds, sla_down_seconds, sla_last_update "
        "FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ) as cursor:
        row = await cursor.fetchone()

    prev_last_status = row["last_status"] if row else None
    consec_up = int(row["consec_up"]) if row else 0
    consec_down = int(row["consec_down"]) if row else 0
    prev_stable_status = row["stable_status"] if row else "unknown"

    # SLA tracking
    sla_month = row["sla_month"] if row else None
    sla_up = int(row["sla_up_seconds"] or 0) if row else 0
    sla_down = int(row["sla_down_seconds"] or 0) if row else 0
    sla_last = int(row["sla_last_update"] or 0) if row else 0

    # Reset SLA counters if new month
    if sla_month != current_month:
        sla_month = current_month
        sla_up = 0
        sla_down = 0
        sla_last = now

    # Calculate time delta and add to appropriate counter
    if sla_last > 0 and row is not None:
        delta = now - sla_last
        if delta > 0:
            # Use stable_status for SLA (more accurate than instant status)
            if prev_stable_status == 'up':
                sla_up += delta
            elif prev_stable_status == 'down':
                sla_down += delta
            # 'unknown' status is not counted (neither up nor down)

    if row is None:
        # Bug #7 fix: symmetric anti-flap — new hosts always start as 'unknown'
        # and must pass threshold cycles to become 'up' or 'down'
        if status == 'up':
            initial_consec_up = 1
            initial_consec_down = 0
        else:
            initial_consec_up = 0
            initial_consec_down = 1

        await conn.execute(
            "INSERT INTO host_states(zone_id, name, type, content, last_status, "
            "last_checked_at, last_changed_at, consec_up, consec_down, "
            "stable_status, stable_changed_at, "
            "sla_month, sla_up_seconds, sla_down_seconds, sla_last_update) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (zone_id, name, type_, content, status, now, now,
             initial_consec_up, initial_consec_down, 'unknown', None,
             current_month, 0, 0, now),
        )
        new_stable_status = 'unknown'
    else:
        changed = (prev_last_status != status)
        if status == 'up':
            consec_up = consec_up + 1
            consec_down = 0
        else:
            consec_down = consec_down + 1
            consec_up = 0

        new_stable_status = prev_stable_status

        current_changed_at = await row_changed_at(conn, zone_id, name, type_, content)
        stable_changed_at = current_changed_at

        if status == 'up' and consec_up >= up_threshold and prev_stable_status != 'up':
            new_stable_status = 'up'
            stable_changed_at = now
        elif status == 'down' and consec_down >= down_threshold and prev_stable_status != 'down':
            new_stable_status = 'down'
            stable_changed_at = now

        await conn.execute(
            "UPDATE host_states SET last_status=?, last_checked_at=?, "
            "last_changed_at=?, consec_up=?, consec_down=?, "
            "stable_status=?, stable_changed_at=?, "
            "sla_month=?, sla_up_seconds=?, sla_down_seconds=?, sla_last_update=? "
            "WHERE zone_id=? AND name=? AND type=? AND content=?",
            (status, now, now if changed else current_changed_at,
             consec_up, consec_down, new_stable_status, stable_changed_at,
             sla_month, sla_up, sla_down, now,
             zone_id, name, type_, content),
        )

    # Bug #3 fix: explicit return variables instead of confusing ternary
    return (prev_last_status or "unknown"), status, prev_stable_status, new_stable_status


async def db_get_records_by_name_types(conn: aiosqlite.Connection, name: str, types: Set[str], zone_id: Optional[str] = None) -> List[aiosqlite.Row]:
    """Bug #6 fix: optionally filter by zone_id to prevent cross-zone mixing."""
    placeholders = ",".join(["?"] * len(types))
    if zone_id:
        query = f"SELECT * FROM dns_records WHERE zone_id=? AND name=? AND type IN ({placeholders})"
        params = (zone_id, name, *list(types))
    else:
        query = f"SELECT * FROM dns_records WHERE name=? AND type IN ({placeholders})"
        params = (name, *list(types))
    async with conn.execute(query, params) as cursor:
        return list(await cursor.fetchall())


# --------------- Domain settings CRUD ---------------


async def db_get_domain_settings(
    conn: aiosqlite.Connection,
) -> List[dict]:
    """Return all active (non-deleted) domain settings as list of dicts."""
    async with conn.execute(
        "SELECT zone_id, hostname, enabled, added_at "
        "FROM domain_settings WHERE deleted=0 ORDER BY hostname"
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"zone_id": r[0], "hostname": r[1],
             "enabled": bool(r[2]), "added_at": r[3]} for r in rows]


async def db_is_domain_deleted(
    conn: aiosqlite.Connection,
    zone_id: str, hostname: str,
) -> bool:
    """Check if domain was soft-deleted."""
    async with conn.execute(
        "SELECT deleted FROM domain_settings WHERE zone_id=? AND hostname=?",
        (zone_id, hostname),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row[0])


async def db_set_domain_enabled(
    conn: aiosqlite.Connection,
    zone_id: str, hostname: str, enabled: bool,
) -> None:
    """Toggle enabled flag for a domain."""
    now = int(time.time())
    await conn.execute(
        "INSERT INTO domain_settings(zone_id, hostname, enabled, added_at) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(zone_id, hostname) DO UPDATE SET enabled=?",
        (zone_id, hostname, int(enabled), now, int(enabled)),
    )
    await conn.commit()


async def db_add_domain(
    conn: aiosqlite.Connection,
    zone_id: str, hostname: str,
    force: bool = False,
) -> bool:
    """Add a new domain to settings (enabled by default).

    Returns True if domain was added, False if it was previously deleted.
    Use force=True to re-add a deleted domain (from UI).
    """
    # Check if domain was soft-deleted
    if not force and await db_is_domain_deleted(conn, zone_id, hostname):
        return False

    now = int(time.time())
    await conn.execute(
        "INSERT INTO domain_settings(zone_id, hostname, enabled, added_at, deleted) "
        "VALUES(?,?,1,?,0) "
        "ON CONFLICT(zone_id, hostname) DO UPDATE SET deleted=0, enabled=1, added_at=?",
        (zone_id, hostname, now, now),
    )
    await conn.commit()
    return True


async def db_remove_domain(
    conn: aiosqlite.Connection,
    zone_id: str, hostname: str,
) -> None:
    """Soft-delete a domain from settings."""
    await conn.execute(
        "UPDATE domain_settings SET deleted=1 WHERE zone_id=? AND hostname=?",
        (zone_id, hostname),
    )
    await conn.commit()
