
#!/usr/bin/env python3
import argparse
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Set, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv


@dataclass
class Config:
    zone_hostname_pairs: List[Tuple[str, str]]  # (zone_id, hostname)
    record_types: Set[str]
    proxied_default: bool
    ping_interval_seconds: int
    sync_interval_minutes: int
    flap_threshold: int
    flap_up_threshold: int
    flap_down_threshold: int
    manage_dns: bool
    db_path: str
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    tg_enabled: bool
    log_level: str = "INFO"


CF_API_BASE = "https://api.cloudflare.com/client/v4"

# Глобальная переменная для graceful shutdown
shutdown_requested = False


def setup_logging(log_level: str = "INFO") -> None:
    """Настройка структурированного логирования"""
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Формат логов с дополнительной информацией
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-5s | %(funcName)-20s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Настройка root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Удаляем существующие handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Отключаем логирование requests для уменьшения шума
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    global shutdown_requested
    logging.info(f"Получен сигнал {signum}, инициируем graceful shutdown...")
    shutdown_requested = True


def register_signal_handlers():
    """Регистрация обработчиков сигналов"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)




# Обратная совместимость с существующими функциями логирования
def info(msg: str) -> None:
    logging.info(msg)


def warn(msg: str) -> None:
    logging.warning(msg)


def err(msg: str) -> None:
    logging.error(msg)


def load_config_from_env() -> Config:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=env_path, override=True)

    import re
    
    # Формат: CF_ZONE_HOSTNAME=zone1:domain1,zone2:domain2
    zone_hostname_raw = os.getenv("CF_ZONE_HOSTNAME") or ""
    if not zone_hostname_raw:
        raise ValueError("CF_ZONE_HOSTNAME обязателен. Формат: zone_id:hostname,zone_id:hostname")
    
    zone_hostname_pairs = []
    for pair in re.split(r"[\s,;]+", zone_hostname_raw):
        if ":" in pair:
            zone_id, hostname = pair.split(":", 1)
            zone_hostname_pairs.append((zone_id.strip(), hostname.strip()))
    
    if not zone_hostname_pairs:
        raise ValueError("CF_ZONE_HOSTNAME должен содержать хотя бы одну пару zone_id:hostname")

    record_types_env = os.getenv("CF_RECORD_TYPES", os.getenv("CF_RECORD_TYPE", "A")).upper()
    record_types = {t.strip() for t in record_types_env.split(",") if t.strip()} & {"A", "AAAA"}
    if not record_types:
        record_types = {"A"}

    proxied_env = os.getenv("CF_PROXIED", "false").strip().lower()
    proxied_default = proxied_env in {"1", "true", "yes", "on"}

    ping_interval_seconds = int(os.getenv("PING_INTERVAL_SECONDS", "10"))
    sync_interval_minutes = int(os.getenv("CF_SYNC_INTERVAL_MINUTES", "3"))
    flap_threshold = int(os.getenv("FLAP_THRESHOLD", "3"))
    flap_up_threshold = int(os.getenv("FLAP_UP_THRESHOLD", "2"))
    flap_down_threshold = int(os.getenv("FLAP_DOWN_THRESHOLD", "3"))
    manage_dns = (os.getenv("CF_MANAGE_DNS", "true").strip().lower() in {"1", "true", "yes", "on"})

    db_path = os.getenv("CF_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cf_dns.db")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    tg_enabled = (os.getenv("TELEGRAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}) and bool(tg_token and tg_chat_id)
    
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    return Config(
        zone_hostname_pairs=zone_hostname_pairs,
        record_types=record_types,
        proxied_default=proxied_default,
        ping_interval_seconds=ping_interval_seconds,
        sync_interval_minutes=sync_interval_minutes,
        flap_threshold=flap_threshold,
        flap_up_threshold=flap_up_threshold,
        flap_down_threshold=flap_down_threshold,
        manage_dns=manage_dns,
        db_path=db_path,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        tg_enabled=tg_enabled,
        log_level=log_level,
    )


# -------------------- Telegram --------------------

def tg_send(cfg: Config, text: str) -> None:
    if not cfg.tg_enabled:
        return
    try:
        url = f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage"
        payload = {
            "chat_id": cfg.tg_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            warn(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        warn(f"Telegram send exception: {e}")


# -------------------- DB --------------------

def db_connect(db_path: str) -> sqlite3.Connection:
    """Подключение к базе данных с обработкой ошибок"""
    try:
        # Создаем директорию для БД если не существует
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logging.info(f"Создана директория для БД: {db_dir}")
        
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        
        # Настройки для производительности и надежности
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA temp_store=MEMORY")
        
        logging.info(f"Подключение к БД установлено: {db_path}")
        return conn
        
    except sqlite3.Error as e:
        logging.error(f"Ошибка подключения к БД {db_path}: {e}")
        raise
    except Exception as e:
        logging.error(f"Неожиданная ошибка при подключении к БД: {e}")
        raise


def db_init(conn: sqlite3.Connection) -> None:
    """Инициализация схемы базы данных с обработкой ошибок"""
    try:
        # Создание таблицы dns_records
        conn.execute(
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dns_records_name_type ON dns_records(name, type);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dns_records_zone_id ON dns_records(zone_id);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dns_records_status ON dns_records(status);
            """
        )
        
        # Создание таблицы host_states
        conn.execute(
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_host_states_name ON host_states(name);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_host_states_stable_status ON host_states(stable_status);
            """
        )
        
        # Миграции для совместимости со старыми схемами
        cols = [r[1] for r in conn.execute("PRAGMA table_info(host_states)").fetchall()]
        
        def ensure_col(name: str, ddl: str) -> None:
            if name not in cols:
                logging.info(f"Добавляем колонку {name} в таблицу host_states")
                conn.execute(f"ALTER TABLE host_states ADD COLUMN {ddl}")
        
        ensure_col("consec_up", "consec_up INTEGER NOT NULL DEFAULT 0")
        ensure_col("consec_down", "consec_down INTEGER NOT NULL DEFAULT 0")
        ensure_col("stable_status", "stable_status TEXT NOT NULL DEFAULT 'unknown'")
        ensure_col("stable_changed_at", "stable_changed_at INTEGER")
        
        conn.commit()
        logging.info("Схема базы данных инициализирована успешно")
        
    except sqlite3.Error as e:
        logging.error(f"Ошибка инициализации БД: {e}")
        conn.rollback()
        raise
    except Exception as e:
        logging.error(f"Неожиданная ошибка при инициализации БД: {e}")
        conn.rollback()
        raise


def db_upsert_record(conn: sqlite3.Connection, rec: dict, status: str = "unknown", ts_val: Optional[int] = None) -> None:
    now = int(time.time())
    conn.execute(
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
    conn.commit()


def db_update_status(conn: sqlite3.Connection, record_id: str, status: str) -> None:
    now = int(time.time())
    conn.execute(
        "UPDATE dns_records SET status=?, last_checked_at=?, updated_at=? WHERE id=?",
        (status, now, now, record_id),
    )
    conn.commit()


def db_upsert_host_state(conn: sqlite3.Connection, zone_id: str, name: str, type_: str, content: str, status: str, up_threshold: int, down_threshold: int) -> Tuple[str, str, str, str]:
    """Update aggregated state per (zone_id, name, type, content).

    Returns a tuple (prev_status, new_status, stable_status) after updating counters.
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT last_status, consec_up, consec_down, stable_status FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ).fetchone()
    prev = row["last_status"] if row else None
    consec_up = int(row["consec_up"]) if row else 0
    consec_down = int(row["consec_down"]) if row else 0
    stable_status = row["stable_status"] if row else "unknown"
    if row is None:
        # При первом появлении инициализируем с учетом текущего статуса
        if status == 'up':
            # Для доступных серверов сразу устанавливаем стабильный статус 'up'
            # если это первый пинг и сервер доступен
            initial_stable = 'up'
            initial_consec_up = up_threshold
            initial_consec_down = 0
        else:
            # Для недоступных серверов начинаем с 'unknown'
            initial_stable = 'unknown'
            initial_consec_up = 0
            initial_consec_down = 1
            
        conn.execute(
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
        # Determine new stable_status based on thresholds for up/down
        new_stable = stable_status
        stable_changed_at = row_changed_at(conn, zone_id, name, type_, content)
        if status == 'up' and consec_up >= up_threshold and stable_status != 'up':
            new_stable = 'up'
            stable_changed_at = now
        elif status == 'down' and consec_down >= down_threshold and stable_status != 'down':
            new_stable = 'down'
            stable_changed_at = now
        conn.execute(
            "UPDATE host_states SET last_status=?, last_checked_at=?, last_changed_at=?, consec_up=?, consec_down=?, stable_status=?, stable_changed_at=? WHERE zone_id=? AND name=? AND type=? AND content=?",
            (status, now, now if changed else row_changed_at(conn, zone_id, name, type_, content), consec_up, consec_down, new_stable, stable_changed_at, zone_id, name, type_, content),
        )
        stable_status = new_stable
    conn.commit()
    return prev or "unknown", status, row["stable_status"] if row else initial_stable, stable_status


def row_changed_at(conn: sqlite3.Connection, zone_id: str, name: str, type_: str, content: str) -> int:
    row = conn.execute(
        "SELECT last_changed_at FROM host_states WHERE zone_id=? AND name=? AND type=? AND content=?",
        (zone_id, name, type_, content),
    ).fetchone()
    return int(row["last_changed_at"]) if row and row["last_changed_at"] is not None else int(time.time())


def db_get_records_by_name_types(conn: sqlite3.Connection, name: str, types: Set[str]) -> List[sqlite3.Row]:
    placeholders = ",".join(["?"] * len(types))
    query = f"SELECT * FROM dns_records WHERE name=? AND type IN ({placeholders})"
    return list(conn.execute(query, (name, *list(types))))


# -------------------- Cloudflare API --------------------

def cf_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


def cf_list_records(zone_id: str, name: str, type_: Optional[str], api_token: str) -> List[dict]:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    params = {"name": name}
    if type_:
        params["type"] = type_
    info(f"🌐 CF API запрос: GET {url} params={params}")
    all_results: List[dict] = []
    page = 1
    while True:
        params.update({"page": page, "per_page": 100})
        resp = requests.get(url, headers=cf_headers(api_token), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Cloudflare API error: {data}")
        results = data.get("result", [])
        info(f"🌐 CF API ответ: страница {page}, найдено {len(results)} записей")
        if results:
            for r in results[:3]:  # Показываем первые 3 записи для отладки
                info(f"🌐 CF запись: {r.get('name')} ({r.get('type')}) -> {r.get('content')}")
        if not results:
            break
        all_results.extend(results)
        total_pages = data.get("result_info", {}).get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
    return all_results


def cf_create_record(zone_id: str, name: str, type_: str, content: str, proxied: bool, ttl: int, api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    payload = {
        "type": type_,
        "name": name,
        "content": content,
        "ttl": ttl,
        "proxied": proxied,
    }
    resp = requests.post(url, headers=cf_headers(api_token), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def cf_update_record(zone_id: str, record_id: str, fields: Dict[str, object], api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = requests.patch(url, headers=cf_headers(api_token), json=fields, timeout=15)
    resp.raise_for_status()
    return resp.json()


def cf_delete_record(zone_id: str, record_id: str, api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = requests.delete(url, headers=cf_headers(api_token), timeout=15)
    resp.raise_for_status()
    return resp.json()


# -------------------- Ping --------------------

def ping_once(address: str, timeout_seconds: int = 2) -> bool:
    try:
        completed = subprocess.run(
            ["ping", "-c", "1", address],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# -------------------- Sync logic --------------------

def sync_from_cloudflare_to_db(cfg: Config, api_token: str, conn: sqlite3.Connection) -> None:
    total_records = 0
    
    for zone_id, hostname in cfg.zone_hostname_pairs:
        info(f"🔍 Синхронизация {hostname} в зоне {zone_id}")
        for rtype in cfg.record_types:
            try:
                records = cf_list_records(zone_id, hostname, rtype, api_token)
                if not records:
                    info(f"❌ Нет записей для {hostname} ({rtype}) в зоне {zone_id}")
                    continue
                info(f"✅ Найдено {len(records)} записей для {hostname} ({rtype})")
                for r in records:
                    info(f"📝 Запись: {r['name']} -> {r.get('content')} (ID: {r['id']})")
                    rec = {
                        "id": r["id"],
                        "zone_id": zone_id,
                        "name": r["name"],
                        "type": r["type"],
                        "content": r.get("content"),
                        "ttl": r.get("ttl", 1) or 1,
                        "proxied": r.get("proxied", False),
                    }
                    db_upsert_record(conn, rec, status="unknown")
                    total_records += 1
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 403:
                    info(f"⚠️ Нет доступа к зоне {zone_id} для домена {hostname} - пропускаем")
                    continue
                else:
                    raise
    info(f"Синхронизация завершена: {total_records} записей обновлено")


def evaluate_and_update_status(conn: sqlite3.Connection, cfg: Config, hostname: str, types: Set[str], on_change) -> Tuple[List[str], List[str], Dict[str, sqlite3.Row]]:
    rows = db_get_records_by_name_types(conn, hostname, types)
    up_set: Set[str] = set()
    down_set: Set[str] = set()
    by_content: Dict[str, sqlite3.Row] = {}
    notified_contents: Set[str] = set()
    ping_cache: Dict[str, bool] = {}
    if not rows:
        warn(f"В БД нет записей для {hostname}")
    processed_contents: Set[str] = set()
    for row in rows:
        content = row["content"]
        by_content[content] = row
        if content in processed_contents:
            continue
        if content in ping_cache:
            is_up = ping_cache[content]
        else:
            is_up = ping_once(content, timeout_seconds=2)
            ping_cache[content] = is_up
        new_status = "up" if is_up else "down"
        # Обновляем все строки с этим content одинаково: один раз считаем стабильность/уведомляем
        related = [r for r in rows if r["content"] == content]
        for r in related:
            prev_status = r["status"]
            if new_status != prev_status or r["last_checked_at"] is None:
                db_update_status(conn, r["id"], new_status)
        # Синхронизируем агрегированное состояние и уведомляем только при смене стабильного статуса (3 подряд)
        sample_row = related[0]
        agg_prev, agg_new, stable_prev, stable_new = db_upsert_host_state(
            conn,
            sample_row["zone_id"],
            sample_row["name"],
            sample_row["type"],
            content,
            new_status,
            up_threshold=cfg.flap_up_threshold,
            down_threshold=cfg.flap_down_threshold,
        )
        # Уведомляем только при реальном изменении стабильного статуса
        # Исключаем случаи инициализации и переходов unknown -> unknown
        if (stable_new != stable_prev and 
            stable_new != 'unknown' and 
            stable_prev != 'unknown' and 
            content not in notified_contents):
            on_change(hostname, content, stable_prev, stable_new, sample_row)
            notified_contents.add(content)
        (up_set if is_up else down_set).add(content)
        processed_contents.add(content)
    up_ips = sorted(up_set)
    down_ips = sorted(down_set)
    return up_ips, down_ips, by_content


def list_host_states(conn: sqlite3.Connection, cfg: Config, hostname: str, zone_id: str) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT content, stable_status FROM host_states WHERE zone_id=? AND name=? AND type IN (" + ",".join(["?"] * len(cfg.record_types)) + ")",
        (zone_id, hostname, *list(cfg.record_types)),
    ).fetchall()
    return {r["content"]: r["stable_status"] for r in rows}


def reconcile_dns(conn: sqlite3.Connection, cfg: Config, api_token: str, hostname: str, up_ips: List[str], by_content: Dict[str, sqlite3.Row], zone_id: str) -> None:
    try:
        existing = cf_list_records(zone_id, hostname, None, api_token)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            info(f"⚠️ Нет доступа к зоне {zone_id} для домена {hostname} в reconcile_dns - пропускаем")
            return
        else:
            raise
    existing_ip_to_record: Dict[str, dict] = {rec["content"]: rec for rec in existing if rec["type"] in cfg.record_types}

    # Используем стабильный статус из host_states: добавляем только IP со stable_status='up'
    states = list_host_states(conn, cfg, hostname, zone_id)
    # Политика:
    # - Никогда не удаляем IP со статусом 'unknown' (сохраняем текущее состояние)
    # - Добавляем новые записи только если stable_status='up'
    if states:
        # Блок удаления: хотим оставить всё, что не 'down'
        keep = {ip for ip, s in states.items() if s != 'down'}
        # Блок добавления: можно добавлять только 'up'
        addable = {ip for ip, s in states.items() if s == 'up'}
        current = set(existing_ip_to_record.keys())
        # desired = то, что должно быть в DNS после reconcile
        # оставляем (keep ∩ current) и добавляем (addable - current)
        desired = (keep & current) | (addable - current)
    else:
        # Нет данных о стабильности — сохраняем текущее состояние без изменений
        desired = set(existing_ip_to_record.keys())
    current = set(existing_ip_to_record.keys())

    to_add = desired - current
    to_remove = current - desired
    to_keep = desired & current

    info(f"План для {hostname}: добавить={sorted(to_add)} удалить={sorted(to_remove)} оставить={sorted(to_keep)}")

    for ip in to_add:
        if ip in by_content:
            row = by_content[ip]
            ttl = int(row["ttl"]) if row["ttl"] is not None else 1
            proxied = bool(row["proxied"]) if row["proxied"] is not None else cfg.proxied_default
        else:
            ttl = 1
            proxied = cfg.proxied_default
        cf_create_record(zone_id, hostname, next(iter(cfg.record_types)), ip, proxied=proxied, ttl=ttl, api_token=api_token)

    for ip in to_remove:
        rec = existing_ip_to_record[ip]
        cf_delete_record(zone_id, rec["id"], api_token)

    for ip in to_keep:
        rec = existing_ip_to_record[ip]
        target_proxied = cfg.proxied_default
        if ip in by_content:
            row = by_content[ip]
            target_proxied = bool(row["proxied"]) if row["proxied"] is not None else cfg.proxied_default
        if bool(rec.get("proxied", False)) != target_proxied:
            cf_update_record(zone_id, rec["id"], {"proxied": target_proxied}, api_token)
    
    # Логируем только если были изменения
    if to_add or to_remove:
        changes = []
        if to_add:
            changes.append(f"добавлено: {to_add}")
        if to_remove:
            changes.append(f"удалено: {to_remove}")
        info(f"DNS изменения для {hostname}: {', '.join(changes)}")


def build_status_summary(conn: sqlite3.Connection, cfg: Config) -> str:
    lines: List[str] = ["📊 <b>Статус DNS</b>", ""]
    
    current_zone = None
    for zone_id, hostname in cfg.zone_hostname_pairs:
        if current_zone != zone_id:
            if current_zone is not None:  # Добавляем разделитель между зонами
                lines.append("")
            lines.append(f"🌐 <b>Зона:</b> <code>{zone_id}</code>")
            current_zone = zone_id
        
        # Сначала ищем в host_states (более актуальные данные)
        states = conn.execute(
            "SELECT content, COALESCE(stable_status, last_status) AS s FROM host_states WHERE zone_id=? AND name=? AND type IN (" + ",".join(["?"] * len(cfg.record_types)) + ")",
            (zone_id, hostname, *list(cfg.record_types)),
        ).fetchall()
        
        if states:
            lines.append(f"  📍 <b>{hostname}</b>")
            # Сортируем по IP адресу
            items = sorted([(r["content"], r["s"]) for r in states])
            for ip, status in items:
                dot = "🟢" if status == "up" else "🔴"
                lines.append(f"    {dot} <code>{ip}</code>")
        else:
            # Fallback: ищем в dns_records если нет в host_states
            rows = db_get_records_by_name_types(conn, hostname, cfg.record_types)
            if rows:
                lines.append(f"  📍 <b>{hostname}</b>")
                # Сортируем по IP адресу и убираем дубликаты
                items = sorted(set([(row["content"], row["status"]) for row in rows]))
                for ip, status in items:
                    dot = "🟢" if status == "up" else "🔴"
                    lines.append(f"    {dot} <code>{ip}</code>")
            else:
                lines.append(f"  📍 <b>{hostname}</b>: <i>записей нет</i>")
    
    return "\n".join(lines)


# -------------------- CLI and main loop --------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloudflare DNS balancer with SQLite state, ICMP ping and Telegram alerts")
    parser.add_argument("--once", action="store_true", help="Run a single sync + ping + optional reconcile and exit")
    parser.add_argument("--no-manage-dns", action="store_true", help="Do not modify DNS at Cloudflare (DB only)")
    return parser.parse_args()


def main() -> None:
    """Главная функция с улучшенной обработкой ошибок и graceful shutdown"""
    global shutdown_requested
    
    try:
        # Парсинг аргументов и загрузка конфигурации
        args = parse_args()
        cfg = load_config_from_env()
        
        # Настройка логирования
        setup_logging(cfg.log_level)
        logging.info("Запуск Cloudflare DNS Load Balancer Bot")
        
        # Регистрация обработчиков сигналов
        register_signal_handlers()
        
        if args.no_manage_dns:
            cfg.manage_dns = False
            logging.info("Режим только мониторинга (DNS изменения отключены)")

        # Проверка API токена
        api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
        if not api_token:
            logging.error("Переменная CLOUDFLARE_API_TOKEN (или CF_API_TOKEN) не установлена")
            sys.exit(2)

        # Подключение к базе данных
        conn = db_connect(cfg.db_path)
        db_init(conn)

        # Статистика запуска
        unique_zones = len(set(pair[0] for pair in cfg.zone_hostname_pairs))
        logging.info(f"Запуск: {unique_zones} зон, {len(cfg.zone_hostname_pairs)} доменов, синхронизация каждые {cfg.sync_interval_minutes}мин")

        # Первоначальная синхронизация
        sync_from_cloudflare_to_db(cfg, api_token, conn)

        # Обработчики изменений статуса
        def on_status_change(hostname: str, ip: str, prev: str, new: str, row: sqlite3.Row) -> None:
            if new == "up":
                text = f"🟢 <b>{hostname}</b> <code>{ip}</code> доступен"
            else:
                text = f"🔴 <b>{hostname}</b> <code>{ip}</code> недоступен"
            tg_send(cfg, text)
            logging.info(f"TG: {text}")

        def silent_on_status_change(hostname: str, ip: str, prev: str, new: str, row: sqlite3.Row) -> None:
            # Не отправляем уведомления во время первого цикла
            pass

        def one_cycle(status_change_handler):
            """Один цикл проверки всех доменов"""
            for zone_id, hostname in cfg.zone_hostname_pairs:
                try:
                    up_ips, down_ips, by_content = evaluate_and_update_status(
                        conn, cfg, hostname, cfg.record_types, status_change_handler
                    )
                    if cfg.manage_dns:
                        reconcile_dns(conn, cfg, api_token, hostname, up_ips, by_content, zone_id)
                except Exception as e:
                    logging.error(f"Ошибка обработки домена {hostname}: {e}")
                    continue

        # Первый цикл без уведомлений
        one_cycle(silent_on_status_change)
        
        # Отправляем сводку статуса
        tg_send(cfg, build_status_summary(conn, cfg))

        if args.once:
            logging.info("Завершено (разовый запуск)")
            return

        # Основной цикл
        cycle_count = 0
        sync_interval_cycles = (cfg.sync_interval_minutes * 60) // cfg.ping_interval_seconds
        logging.info(f"Синхронизация с CF каждые {cfg.sync_interval_minutes} мин")

        while not shutdown_requested:
            try:
                one_cycle(on_status_change)
                cycle_count += 1
                
                # Периодическая синхронизация с Cloudflare
                if cycle_count >= sync_interval_cycles:
                    logging.info("Синхронизация с CF...")
                    sync_from_cloudflare_to_db(cfg, api_token, conn)
                    cycle_count = 0
                    
            except Exception as e:
                logging.error(f"Ошибка в основном цикле: {e}")
                # Продолжаем работу даже при ошибках
                
            # Проверяем shutdown между циклами
            for _ in range(cfg.ping_interval_seconds):
                if shutdown_requested:
                    break
                time.sleep(1)
        
        logging.info("Graceful shutdown завершен")
        
    except KeyboardInterrupt:
        logging.info("Получен сигнал прерывания")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Закрытие соединения с БД
        try:
            if 'conn' in locals():
                conn.close()
                logging.info("Соединение с БД закрыто")
        except Exception as e:
            logging.error(f"Ошибка при закрытии БД: {e}")


if __name__ == "__main__":
    main()
