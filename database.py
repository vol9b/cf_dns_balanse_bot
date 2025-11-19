import os
import time
import logging
import aiosqlite
from typing import List, Set, Tuple, Optional, Dict

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
    await conn.commit()

async def db_update_status(conn: aiosqlite.Connection, record_id: str, status: str) -> None:
    now = int(time.time())
    await conn.execute(
        "UPDATE dns_records SET status=?, last_checked_at=?, updated_at=? WHERE id=?",
        (status, now, now, record_id),
    )
    await conn.commit()

async def row_changed_at(conn: aiosqlite.Connection, zone_id: str, name: str, type_: str, content: str) -> int:
    async with conn.execute(
        "SELECT last_changed_at FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["last_changed_at"]) if row and row["last_changed_at"] is not None else int(time.time())

async def db_upsert_host_state(conn: aiosqlite.Connection, zone_id: str, name: str, type_: str, content: str, status: str, up_threshold: int, down_threshold: int) -> Tuple[str, str, str, str]:
    """Update aggregated state per (zone_id, name, type, content).
    Returns a tuple (prev_status, new_status, stable_status) after updating counters.
    """
    now = int(time.time())
    async with conn.execute(
        "SELECT last_status, consec_up, consec_down, stable_status FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ) as cursor:
        row = await cursor.fetchone()

    prev = row["last_status"] if row else None
    consec_up = int(row["consec_up"]) if row else 0
    consec_down = int(row["consec_down"]) if row else 0
    stable_status = row["stable_status"] if row else "unknown"
    
    if row is None:
        # При первом появлении инициализируем с учетом текущего статуса
        if status == 'up':
            initial_stable = 'up'
            initial_consec_up = up_threshold
            initial_consec_down = 0
        else:
            initial_stable = 'unknown'
            initial_consec_up = 0
            initial_consec_down = 1
            
        await conn.execute(
            "INSERT INTO host_states(zone_id, name, type, content, last_status, last_checked_at, last_changed_at, consec_up, consec_down, stable_status, stable_changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (zone_id, name, type_, content, status, now, now, initial_consec_up, initial_consec_down, initial_stable, None),
        )
    else:
        changed = (prev != status)
        if status == 'up':
            consec_up = consec_up + 1
            consec_down = 0
        else:
            consec_down = consec_down + 1
            consec_up = 0
            
        new_stable = stable_status
        
        current_changed_at = await row_changed_at(conn, zone_id, name, type_, content)
        stable_changed_at = current_changed_at # пока не меняем

        if status == 'up' and consec_up >= up_threshold and stable_status != 'up':
            new_stable = 'up'
            stable_changed_at = now
        elif status == 'down' and consec_down >= down_threshold and stable_status != 'down':
            new_stable = 'down'
            stable_changed_at = now
            
        await conn.execute(
            "UPDATE host_states SET last_status=?, last_checked_at=?, last_changed_at=?, consec_up=?, consec_down=?, stable_status=?, stable_changed_at=? WHERE zone_id=? AND name=? AND type=? AND content=?",
            (status, now, now if changed else current_changed_at, consec_up, consec_down, new_stable, stable_changed_at, zone_id, name, type_, content),
        )
        stable_status = new_stable
        
    await conn.commit()
    return prev or "unknown", status, row["stable_status"] if row else initial_stable, stable_status

async def db_get_records_by_name_types(conn: aiosqlite.Connection, name: str, types: Set[str]) -> List[aiosqlite.Row]:
    placeholders = ",".join(["?"] * len(types))
    query = f"SELECT * FROM dns_records WHERE name=? AND type IN ({placeholders})"
    async with conn.execute(query, (name, *list(types))) as cursor:
        return await cursor.fetchall()
