
#!/usr/bin/env python3
import argparse
import os
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
    zone_ids: List[str]
    hostnames: List[str]
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


CF_API_BASE = "https://api.cloudflare.com/client/v4"


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def info(msg: str) -> None:
    print(f"[{ts()}] INFO  {msg}")


def warn(msg: str) -> None:
    print(f"[{ts()}] WARN  {msg}")


def err(msg: str) -> None:
    print(f"[{ts()}] ERROR {msg}", file=sys.stderr)


def load_config_from_env() -> Config:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=env_path, override=True)

    # Поддержка нескольких зон через запятую
    import re
    zone_id_raw = os.getenv("CF_ZONE_ID") or os.getenv("CLOUDFLARE_ZONE_ID") or ""
    zone_ids = [z.strip() for z in re.split(r"[\s,;]+", zone_id_raw) if z.strip()]

    # Источник доменов: CF_HOSTNAMES (основной) или CF_HOSTNAME (наследие).
    raw = os.getenv("CF_HOSTNAME") or ""
    hostnames = [h.strip() for h in re.split(r"[\s,;]+", raw) if h.strip()]

    record_types_env = os.getenv("CF_RECORD_TYPES", os.getenv("CF_RECORD_TYPE", "A")).upper()
    record_types = {t.strip() for t in record_types_env.split(",") if t.strip()} & {"A", "AAAA"}
    if not record_types:
        record_types = {"A"}

    proxied_env = os.getenv("CF_PROXIED", "true").strip().lower()
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

    if not zone_ids or not hostnames:
        err("CF_ZONE_ID и CF_HOSTNAME обязательны. Проверь .env")
        sys.exit(2)

    return Config(
        zone_ids=zone_ids,
        hostnames=hostnames,
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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def db_init(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dns_records_name_type ON dns_records(name, type);
        """
    )
    # Aggregated unique host state per (zone_id, name, type, content)
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
    # Migrations for older schema of host_states (if created before)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(host_states)").fetchall()]
    def ensure_col(name: str, ddl: str) -> None:
        if name not in cols:
            conn.execute(f"ALTER TABLE host_states ADD COLUMN {ddl}")
    ensure_col("consec_up", "consec_up INTEGER NOT NULL DEFAULT 0")
    ensure_col("consec_down", "consec_down INTEGER NOT NULL DEFAULT 0")
    ensure_col("stable_status", "stable_status TEXT NOT NULL DEFAULT 'unknown'")
    ensure_col("stable_changed_at", "stable_changed_at INTEGER")
    conn.commit()


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
        # При первом появлении не фиксируем stable_status сразу, считаем серии
        conn.execute(
            "INSERT INTO host_states(zone_id, name, type, content, last_status, last_checked_at, last_changed_at, consec_up, consec_down, stable_status, stable_changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (zone_id, name, type_, content, status, now, now, 1 if status == 'up' else 0, 1 if status == 'down' else 0, 'unknown', None),
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
    return prev or "unknown", status, row["stable_status"] if row else 'unknown', stable_status


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
    for zone_id in cfg.zone_ids:
        for hostname in cfg.hostnames:
            for rtype in cfg.record_types:
                records = cf_list_records(zone_id, hostname, rtype, api_token)
                if not records:
                    continue
                for r in records:
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
        if stable_new != stable_prev and stable_new != 'unknown' and content not in notified_contents:
            on_change(hostname, content, stable_prev if stable_prev != 'unknown' else ('up' if agg_prev == 'up' else 'down'), stable_new, sample_row)
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
    existing = cf_list_records(zone_id, hostname, None, api_token)
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
    lines: List[str] = ["📊 Статус DNS"]
    for zone_id in cfg.zone_ids:
        lines.append(f"🌐 Зона: {zone_id}")
        for hostname in cfg.hostnames:
            rows = db_get_records_by_name_types(conn, hostname, cfg.record_types)
            if not rows:
                lines.append(f"• <b>{hostname}</b>: записей нет")
                continue
            lines.append(f"• <b>{hostname}</b>:")
            # Берём статусы из host_states, если доступны, иначе из dns_records
            states = conn.execute(
                "SELECT content, COALESCE(stable_status, last_status) AS s FROM host_states WHERE zone_id=? AND name=? AND type IN (" + ",".join(["?"] * len(cfg.record_types)) + ")",
                (zone_id, hostname, *list(cfg.record_types)),
            ).fetchall()
        if states:
            items = sorted({(r["content"], r["s"]) for r in states})
        else:
            items = sorted({(r["content"], r["status"]) for r in rows})
        for ip, s in items:
            dot = "🟢" if s == "up" else "🔴"
            lines.append(f"  {dot} <code>{ip}</code>")
    return "\n".join(lines)


# -------------------- CLI and main loop --------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloudflare DNS balancer with SQLite state, ICMP ping and Telegram alerts")
    parser.add_argument("--once", action="store_true", help="Run a single sync + ping + optional reconcile and exit")
    parser.add_argument("--no-manage-dns", action="store_true", help="Do not modify DNS at Cloudflare (DB only)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config_from_env()

    if args.no_manage_dns:
        cfg.manage_dns = False

    api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
    if not api_token:
        err("Переменная CLOUDFLARE_API_TOKEN (или CF_API_TOKEN) не установлена")
        sys.exit(2)

    conn = db_connect(cfg.db_path)
    db_init(conn)

    info(f"Запуск: {len(cfg.zone_ids)} зон, {len(cfg.hostnames)} доменов, синхронизация каждые {cfg.sync_interval_minutes}мин")

    sync_from_cloudflare_to_db(cfg, api_token, conn)

    def on_status_change(hostname: str, ip: str, prev: str, new: str, row: sqlite3.Row) -> None:
        if new == "up":
            text = f"🟢 <b>{hostname}</b> <code>{ip}</code> доступен"
        else:
            text = f"🔴 <b>{hostname}</b> <code>{ip}</code> недоступен"
        tg_send(cfg, text)
        info(f"TG: {text}")

    def one_cycle():
        for zone_id in cfg.zone_ids:
            for hostname in cfg.hostnames:
                up_ips, down_ips, by_content = evaluate_and_update_status(conn, cfg, hostname, cfg.record_types, on_status_change)
                if cfg.manage_dns:
                    reconcile_dns(conn, cfg, api_token, hostname, up_ips, by_content, zone_id)

    # Initial status summary message after first evaluation pass
    one_cycle()
    tg_send(cfg, build_status_summary(conn, cfg))

    if args.once:
        info("Завершено (разовый запуск)")
        return

    # Счетчик циклов для периодической синхронизации
    cycle_count = 0
    sync_interval_cycles = (cfg.sync_interval_minutes * 60) // cfg.ping_interval_seconds
    info(f"Синхронизация с CF каждые {cfg.sync_interval_minutes} мин")

    while True:
        try:
            one_cycle()
            cycle_count += 1
            
            # Периодическая синхронизация с Cloudflare
            if cycle_count >= sync_interval_cycles:
                info(f"Синхронизация с CF...")
                sync_from_cloudflare_to_db(cfg, api_token, conn)
                cycle_count = 0
                
        except Exception as e:
            err(f"Ошибка цикла: {e}")
        time.sleep(cfg.ping_interval_seconds)


if __name__ == "__main__":
    main()
