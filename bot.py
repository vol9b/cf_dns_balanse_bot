#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import List, Set, Dict, Tuple

import aiohttp
import aiosqlite

from config import Config, load_config_from_env
from database import (
    db_connect, db_init, db_upsert_record, db_update_status, 
    db_upsert_host_state, db_get_records_by_name_types
)
from cloudflare import (
    cf_list_records, cf_create_record, cf_update_record, cf_delete_record
)
from notifications import tg_send
from health import ping_once

# Глобальная переменная для graceful shutdown
shutdown_event = asyncio.Event()

def setup_logging(log_level: str = "INFO") -> None:
    """Настройка структурированного логирования"""
    level = getattr(logging, log_level.upper(), logging.INFO)
    
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-5s | %(funcName)-20s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

def info(msg: str) -> None:
    logging.info(msg)

def warn(msg: str) -> None:
    logging.warning(msg)

def err(msg: str) -> None:
    logging.error(msg)

# -------------------- Sync logic --------------------

async def sync_from_cloudflare_to_db(cfg: Config, api_token: str, conn: aiosqlite.Connection, session: aiohttp.ClientSession) -> None:
    total_records = 0
    
    for zone_id, hostname in cfg.zone_hostname_pairs:
        info(f"🔍 Синхронизация {hostname} в зоне {zone_id}")
        for rtype in cfg.record_types:
            try:
                records = await cf_list_records(session, zone_id, hostname, rtype, api_token)
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
                    await db_upsert_record(conn, rec, status="unknown")
                    total_records += 1
            except aiohttp.ClientResponseError as e:
                if e.status == 403:
                    info(f"⚠️ Нет доступа к зоне {zone_id} для домена {hostname} - пропускаем")
                    continue
                else:
                    raise
    info(f"Синхронизация завершена: {total_records} записей обновлено")


async def evaluate_and_update_status(conn: aiosqlite.Connection, cfg: Config, hostname: str, types: Set[str], on_change) -> Tuple[List[str], List[str], Dict[str, aiosqlite.Row]]:
    rows = await db_get_records_by_name_types(conn, hostname, types)
    info(f"🔍 Проверяем {hostname}: найдено {len(rows)} записей в БД для типов {types}")
    up_set: Set[str] = set()
    down_set: Set[str] = set()
    by_content: Dict[str, aiosqlite.Row] = {}
    notified_contents: Set[str] = set()
    
    if not rows:
        warn(f"❌ В БД нет записей для {hostname} (типы: {types})")
        return [], [], {}
    
    unique_ips = {row["content"] for row in rows}
    
    ping_tasks = [ping_once(ip, timeout_seconds=2) for ip in unique_ips]
    ping_results = await asyncio.gather(*ping_tasks)
    ping_map = dict(zip(unique_ips, ping_results))
    
    processed_contents: Set[str] = set()
    
    for row in rows:
        content = row["content"]
        by_content[content] = row
        if content in processed_contents:
            continue
            
        is_up = ping_map.get(content, False)
        info(f"🏓 Пинг {content}: {'✅ доступен' if is_up else '❌ недоступен'}")
        
        new_status = "up" if is_up else "down"
        
        related = [r for r in rows if r["content"] == content]
        for r in related:
            prev_status = r["status"]
            if new_status != prev_status or r["last_checked_at"] is None:
                await db_update_status(conn, r["id"], new_status)
        
        sample_row = related[0]
        agg_prev, agg_new, stable_prev, stable_new = await db_upsert_host_state(
            conn,
            sample_row["zone_id"],
            sample_row["name"],
            sample_row["type"],
            content,
            new_status,
            up_threshold=cfg.flap_up_threshold,
            down_threshold=cfg.flap_down_threshold,
        )
        
        if (stable_new != stable_prev and 
            stable_new != 'unknown' and 
            stable_prev != 'unknown' and 
            content not in notified_contents):
            await on_change(hostname, content, stable_prev, stable_new, sample_row)
            notified_contents.add(content)
            
        (up_set if is_up else down_set).add(content)
        processed_contents.add(content)
        
    up_ips = sorted(up_set)
    down_ips = sorted(down_set)
    return up_ips, down_ips, by_content


async def list_host_states(conn: aiosqlite.Connection, cfg: Config, hostname: str, zone_id: str) -> Dict[str, str]:
    placeholders = ",".join(["?"] * len(cfg.record_types))
    query = f"SELECT content, stable_status FROM host_states WHERE zone_id=? AND name=? AND type IN ({placeholders})"
    async with conn.execute(query, (zone_id, hostname, *list(cfg.record_types))) as cursor:
        rows = await cursor.fetchall()
    return {r["content"]: r["stable_status"] for r in rows}


async def reconcile_dns(conn: aiosqlite.Connection, cfg: Config, api_token: str, hostname: str, up_ips: List[str], by_content: Dict[str, aiosqlite.Row], zone_id: str, session: aiohttp.ClientSession) -> None:
    try:
        existing = await cf_list_records(session, zone_id, hostname, None, api_token)
    except aiohttp.ClientResponseError as e:
        if e.status == 403:
            info(f"⚠️ Нет доступа к зоне {zone_id} для домена {hostname} в reconcile_dns - пропускаем")
            return
        else:
            raise
            
    existing_ip_to_record: Dict[str, dict] = {rec["content"]: rec for rec in existing if rec["type"] in cfg.record_types}

    up_ips_set = set(up_ips)
    current = set(existing_ip_to_record.keys())
    
    states = await list_host_states(conn, cfg, hostname, zone_id)
    
    if states:
        to_add_candidates = up_ips_set - current
        down_ips = current - up_ips_set
        stable_unknown = {ip for ip, s in states.items() if s == 'unknown'}
        to_remove_candidates = down_ips - stable_unknown
        desired = (current - to_remove_candidates) | to_add_candidates
    else:
        desired = up_ips_set

    to_add = desired - current
    to_remove = current - desired
    to_keep = desired & current

    info(f"🔍 Пинг результаты для {hostname}: доступные={sorted(up_ips)} всего_в_DNS={len(current)}")
    info(f"📋 План для {hostname}: добавить={sorted(to_add)} удалить={sorted(to_remove)} оставить={sorted(to_keep)}")

    for ip in to_add:
        if ip in by_content:
            row = by_content[ip]
            ttl = int(row["ttl"]) if row["ttl"] is not None else 1
            proxied = bool(row["proxied"]) if row["proxied"] is not None else cfg.proxied_default
        else:
            ttl = 1
            proxied = cfg.proxied_default
        await cf_create_record(session, zone_id, hostname, next(iter(cfg.record_types)), ip, proxied=proxied, ttl=ttl, api_token=api_token)

    for ip in to_remove:
        rec = existing_ip_to_record[ip]
        await cf_delete_record(session, zone_id, rec["id"], api_token)

    for ip in to_keep:
        rec = existing_ip_to_record[ip]
        target_proxied = cfg.proxied_default
        if ip in by_content:
            row = by_content[ip]
            target_proxied = bool(row["proxied"]) if row["proxied"] is not None else cfg.proxied_default
        if bool(rec.get("proxied", False)) != target_proxied:
            await cf_update_record(session, zone_id, rec["id"], {"proxied": target_proxied}, api_token)
    
    if to_add or to_remove:
        changes = []
        if to_add:
            changes.append(f"добавлено: {to_add}")
        if to_remove:
            changes.append(f"удалено: {to_remove}")
        info(f"DNS изменения для {hostname}: {', '.join(changes)}")


async def build_status_summary(conn: aiosqlite.Connection, cfg: Config) -> str:
    lines: List[str] = ["📊 <b>Статус DNS</b>", ""]
    
    current_zone = None
    for zone_id, hostname in cfg.zone_hostname_pairs:
        if current_zone != zone_id:
            if current_zone is not None:
                lines.append("")
            lines.append(f"🌐 <b>Зона:</b> <code>{zone_id}</code>")
            current_zone = zone_id
        
        placeholders = ",".join(["?"] * len(cfg.record_types))
        query = f"SELECT content, COALESCE(stable_status, last_status) AS s FROM host_states WHERE zone_id=? AND name=? AND type IN ({placeholders})"
        async with conn.execute(query, (zone_id, hostname, *list(cfg.record_types))) as cursor:
            states = await cursor.fetchall()
        
        if states:
            lines.append(f"  📍 <b>{hostname}</b>")
            items = sorted([(r["content"], r["s"]) for r in states])
            for ip, status in items:
                dot = "🟢" if status == "up" else "🔴"
                lines.append(f"    {dot} <code>{ip}</code>")
        else:
            rows = await db_get_records_by_name_types(conn, hostname, cfg.record_types)
            if rows:
                lines.append(f"  📍 <b>{hostname}</b>")
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


async def main_async() -> None:
    args = parse_args()
    cfg = load_config_from_env()
    setup_logging(cfg.log_level)
    
    logging.info("Запуск Cloudflare DNS Load Balancer Bot (Async)")
    
    if args.no_manage_dns:
        cfg.manage_dns = False
        logging.info("Режим только мониторинга (DNS изменения отключены)")

    api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
    if not api_token:
        logging.error("Переменная CLOUDFLARE_API_TOKEN (или CF_API_TOKEN) не установлена")
        sys.exit(2)

    # Настройка graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    conn = await db_connect(cfg.db_path)
    await db_init(conn)

    unique_zones = len(set(pair[0] for pair in cfg.zone_hostname_pairs))
    logging.info(f"Запуск: {unique_zones} зон, {len(cfg.zone_hostname_pairs)} доменов, синхронизация каждые {cfg.sync_interval_minutes}мин")

    async with aiohttp.ClientSession() as session:
        # Первоначальная синхронизация
        await sync_from_cloudflare_to_db(cfg, api_token, conn, session)

        async def on_status_change(hostname: str, ip: str, prev: str, new: str, row: aiosqlite.Row) -> None:
            if new == "up":
                text = f"🟢 <b>{hostname}</b> <code>{ip}</code> доступен"
            else:
                text = f"🔴 <b>{hostname}</b> <code>{ip}</code> недоступен"
            await tg_send(cfg, session, text)
            logging.info(f"TG: {text}")

        async def silent_on_status_change(hostname: str, ip: str, prev: str, new: str, row: aiosqlite.Row) -> None:
            pass

        async def one_cycle(status_change_handler):
            info(f"🔄 Начинаем цикл проверки {len(cfg.zone_hostname_pairs)} доменов")
            for zone_id, hostname in cfg.zone_hostname_pairs:
                try:
                    info(f"🎯 Обрабатываем домен: {hostname} (зона: {zone_id})")
                    up_ips, down_ips, by_content = await evaluate_and_update_status(
                        conn, cfg, hostname, cfg.record_types, status_change_handler
                    )
                    if cfg.manage_dns:
                        await reconcile_dns(conn, cfg, api_token, hostname, up_ips, by_content, zone_id, session)
                except Exception as e:
                    logging.error(f"Ошибка обработки домена {hostname}: {e}")
                    continue

        # Первый цикл
        await one_cycle(silent_on_status_change)
        await tg_send(cfg, session, await build_status_summary(conn, cfg))

        if args.once:
            logging.info("Завершено (разовый запуск)")
            await conn.close()
            return

        cycle_count = 0
        sync_interval_cycles = (cfg.sync_interval_minutes * 60) // cfg.ping_interval_seconds
        logging.info(f"Синхронизация с CF каждые {cfg.sync_interval_minutes} мин")

        while not shutdown_event.is_set():
            try:
                start_time = time.time()
                await one_cycle(on_status_change)
                cycle_count += 1
                
                if cycle_count >= sync_interval_cycles:
                    logging.info("Синхронизация с CF...")
                    await sync_from_cloudflare_to_db(cfg, api_token, conn, session)
                    cycle_count = 0
                
                elapsed = time.time() - start_time
                sleep_time = max(0, cfg.ping_interval_seconds - elapsed)
                
                # Ждем sleep_time или события shutdown
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_time)
                except asyncio.TimeoutError:
                    pass # Таймаут истек, продолжаем цикл
                    
            except Exception as e:
                logging.error(f"Ошибка в основном цикле: {e}")
                await asyncio.sleep(5)

    await conn.close()
    logging.info("Graceful shutdown завершен")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
