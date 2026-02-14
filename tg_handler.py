"""
Telegram Bot UI handler — inline keyboards for domain management.
Uses raw Bot API via aiohttp (no external telegram lib needed).
Premium emoji support with fallback to standard emoji.
"""
import asyncio
import logging
import os
import re
import time
from typing import Optional, Set, List, Tuple, Dict

import aiohttp
import aiosqlite

from config import Config
from database import (
    db_get_domain_settings, db_set_domain_enabled,
    db_add_domain, db_remove_domain, db_upsert_record,
)
from notifications import (
    tg_send_with_keyboard, tg_edit_message, tg_answer_callback,
)
from cloudflare import cf_list_records

info = logging.info
warn = logging.warning


# ────────────────────── Premium Emoji System ──────────────────────

# Default emoji fallbacks (used when premium emoji not configured)
EMOJI = {
    # Status indicators
    "online": "🟢",
    "offline": "🔴",
    "warning": "🟡",
    "unknown": "⚪",

    # UI elements
    "dashboard": "📊",
    "settings": "⚙️",
    "add": "➕",
    "delete": "🗑",
    "back": "◀️",
    "refresh": "🔄",
    "check": "✅",
    "cross": "❌",
    "globe": "🌐",
    "server": "🖥",
    "dns": "📡",
    "clock": "🕐",
    "chart": "📈",
    "shield": "🛡",
    "bolt": "⚡",
    "link": "🔗",
    "pin": "📍",
    "zone": "🗂",
    "info": "ℹ️",
    "star": "⭐",
    "fire": "🔥",
    "rocket": "🚀",
}


def _e(key: str, cfg: Optional[Config] = None) -> str:
    """Get emoji - premium if configured, otherwise fallback."""
    if cfg and cfg.tg_custom_emoji_ids:
        custom_id = cfg.tg_custom_emoji_ids.get(key)
        if custom_id:
            # Telegram custom emoji format
            return f'<tg-emoji emoji-id="{custom_id}">{EMOJI.get(key, "")}</tg-emoji>'
    return EMOJI.get(key, "")


def _progress_bar(value: int, total: int, width: int = 10) -> str:
    """Create a visual progress bar."""
    if total == 0:
        return "░" * width
    filled = int((value / total) * width)
    return "▓" * filled + "░" * (width - filled)


def _format_uptime(seconds: int) -> str:
    """Format seconds to human readable uptime."""
    if seconds < 60:
        return f"{seconds}с"
    elif seconds < 3600:
        return f"{seconds // 60}м"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}ч {mins}м"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}д {hours}ч"


def _calc_sla(up_seconds: int, down_seconds: int) -> Tuple[float, str]:
    """Calculate SLA percentage and return (value, emoji_key).

    SLA = uptime / (uptime + downtime) * 100
    Returns emoji_key based on thresholds:
    - 99.9%+ = online (green)
    - 99.0%+ = warning (yellow)
    - <99.0% = offline (red)
    """
    total = up_seconds + down_seconds
    if total == 0:
        return 0.0, "unknown"

    sla = (up_seconds / total) * 100

    if sla >= 99.9:
        return sla, "online"
    elif sla >= 99.0:
        return sla, "warning"
    else:
        return sla, "offline"


def _format_sla(sla: float) -> str:
    """Format SLA percentage for display."""
    if sla >= 99.99:
        return "99.99%"
    elif sla >= 99.9:
        return f"{sla:.2f}%"
    elif sla >= 99.0:
        return f"{sla:.1f}%"
    else:
        return f"{sla:.1f}%"


# ────────────────────── Users waiting state ──────────────────────

_waiting_for_domain: Set[int] = set()


# ────────────────────── CF Validation ──────────────────────

async def _validate_and_sync_domain(
    session: aiohttp.ClientSession,
    conn: aiosqlite.Connection,
    cfg: Config,
    zone_id: str,
    hostname: str,
) -> Tuple[bool, str, List[str]]:
    """Validate zone_id via CF API and sync DNS records."""
    api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
    if not api_token:
        return False, f"{_e('cross')} CF API токен не настроен", []

    found_ips: List[str] = []

    try:
        for rtype in cfg.record_types:
            records = await cf_list_records(
                session, zone_id, hostname, rtype, api_token
            )
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
                await db_upsert_record(conn, rec, status="unknown")
                if r.get("content"):
                    found_ips.append(f"{r['content']} ({r['type']})")

        await conn.commit()

        if found_ips:
            return True, f"{_e('check')} Найдено {len(found_ips)} записей", found_ips
        else:
            return True, f"{_e('warning')} DNS записей не найдено", []

    except aiohttp.ClientResponseError as e:
        if e.status == 403:
            return False, f"{_e('cross')} Нет доступа к зоне", []
        elif e.status == 404:
            return False, f"{_e('cross')} Зона не найдена", []
        else:
            return False, f"{_e('cross')} Ошибка CF API: {e.status}", []
    except Exception as e:
        warn(f"CF API error: {e}")
        return False, f"{_e('cross')} Ошибка: {e}", []


# ────────────────────── Keyboard Builders ──────────────────────

def _btn(
    text: str, callback_data: str,
    style: Optional[str] = None,
    emoji_key: Optional[str] = None,
    cfg: Optional[Config] = None,
) -> dict:
    """Build inline button with API 9.4 features."""
    btn = {"text": text, "callback_data": callback_data}
    if style:
        btn["style"] = style
    if emoji_key and cfg and cfg.tg_custom_emoji_ids:
        eid = cfg.tg_custom_emoji_ids.get(emoji_key)
        if eid:
            btn["icon_custom_emoji_id"] = eid
    return btn


def _kb_main_menu(cfg: Config) -> List[List[dict]]:
    """Main menu keyboard with styled buttons."""
    return [
        [_btn(f"{EMOJI['dashboard']} Статус", "status", style="primary", emoji_key="dashboard", cfg=cfg)],
        [_btn(f"{EMOJI['settings']} Домены", "menu:domains", style="primary", emoji_key="settings", cfg=cfg)],
        [_btn(f"{EMOJI['add']} Добавить", "add_domain", style="success", emoji_key="add", cfg=cfg)],
        [_btn(f"{EMOJI['refresh']} Обновить", "refresh", style="default", emoji_key="refresh", cfg=cfg)],
    ]


def _kb_status(cfg: Config) -> List[List[dict]]:
    """Status page keyboard."""
    return [
        [
            _btn(f"{EMOJI['refresh']} Обновить", "status", style="primary", emoji_key="refresh", cfg=cfg),
        ],
        [
            _btn(f"{EMOJI['back']} Меню", "menu:main", style="default", emoji_key="back", cfg=cfg),
        ],
    ]


async def _kb_domain_list(conn: aiosqlite.Connection, cfg: Config) -> List[List[dict]]:
    """Domain management keyboard with toggle and delete."""
    settings = await db_get_domain_settings(conn)
    settings_map = {(d["zone_id"], d["hostname"]): d["enabled"] for d in settings}

    rows = []

    # Domains from config
    for zone_id, hostname in cfg.zone_hostname_pairs:
        enabled = settings_map.get((zone_id, hostname), True)

        icon = EMOJI["online"] if enabled else EMOJI["offline"]
        style = "success" if enabled else "danger"
        emoji = "online" if enabled else "offline"

        # Shorten hostname if too long
        display_name = hostname if len(hostname) <= 20 else hostname[:17] + "..."

        rows.append([
            _btn(f"{icon} {display_name}", f"toggle:{zone_id}:{hostname}",
                 style=style, emoji_key=emoji, cfg=cfg),
            _btn(EMOJI["delete"], f"delete:{zone_id}:{hostname}",
                 style="danger", emoji_key="delete", cfg=cfg),
        ])

    # Domains from DB not in config
    for d in settings:
        key = (d["zone_id"], d["hostname"])
        if key not in [(z, h) for z, h in cfg.zone_hostname_pairs]:
            icon = EMOJI["online"] if d["enabled"] else EMOJI["offline"]
            style = "success" if d["enabled"] else "danger"
            display_name = d['hostname'] if len(d['hostname']) <= 18 else d['hostname'][:15] + "..."

            rows.append([
                _btn(f"{icon} {display_name} {EMOJI['settings']}",
                     f"toggle:{d['zone_id']}:{d['hostname']}",
                     style=style, cfg=cfg),
                _btn(EMOJI["delete"], f"delete:{d['zone_id']}:{d['hostname']}",
                     style="danger", cfg=cfg),
            ])

    # Navigation
    rows.append([
        _btn(f"{EMOJI['add']} Добавить", "add_domain", style="success", emoji_key="add", cfg=cfg),
    ])
    rows.append([
        _btn(f"{EMOJI['back']} Меню", "menu:main", style="default", emoji_key="back", cfg=cfg),
    ])

    return rows


def _kb_confirm_delete(zone_id: str, hostname: str, cfg: Config) -> List[List[dict]]:
    """Delete confirmation keyboard."""
    return [
        [
            _btn(f"{EMOJI['check']} Удалить", f"confirm_delete:{zone_id}:{hostname}",
                 style="danger", emoji_key="check", cfg=cfg),
            _btn(f"{EMOJI['cross']} Отмена", "menu:domains",
                 style="default", emoji_key="cross", cfg=cfg),
        ],
    ]


def _kb_add_domain(cfg: Config) -> List[List[dict]]:
    """Add domain keyboard."""
    return [
        [_btn(f"{EMOJI['cross']} Отмена", "menu:main", style="default", emoji_key="cross", cfg=cfg)],
    ]


# ────────────────────── Text Builders ──────────────────────

def _main_menu_text(cfg: Config) -> str:
    """Main menu welcome text."""
    lines = [
        f"{_e('globe', cfg)} <b>DNS Load Balancer</b>",
        "",
        f"{_e('shield', cfg)} Cloudflare DNS балансировщик",
        f"{_e('bolt', cfg)} Автоматическое переключение",
        "",
        f"<i>Выберите действие:</i>",
    ]
    return "\n".join(lines)


async def _status_text(conn: aiosqlite.Connection, cfg: Config) -> str:
    """Build comprehensive status dashboard."""
    now = int(time.time())

    # Gather statistics
    total_domains = len(cfg.zone_hostname_pairs)
    total_servers = 0
    online_servers = 0
    offline_servers = 0
    unknown_servers = 0

    domain_stats: List[Dict] = []

    settings = await db_get_domain_settings(conn)
    settings_map = {(d["zone_id"], d["hostname"]): d["enabled"] for d in settings}

    for zone_id, hostname in cfg.zone_hostname_pairs:
        enabled = settings_map.get((zone_id, hostname), True)

        # Get host states
        placeholders = ",".join(["?"] * len(cfg.record_types))
        query = f"""
            SELECT content, stable_status, last_status, last_checked_at, stable_changed_at,
                   sla_up_seconds, sla_down_seconds, sla_month
            FROM host_states
            WHERE zone_id=? AND name=? AND type IN ({placeholders})
        """
        async with conn.execute(query, (zone_id, hostname, *list(cfg.record_types))) as cursor:
            states = await cursor.fetchall()

        servers = []
        for row in states:
            status = row["stable_status"] or row["last_status"] or "unknown"
            last_check = row["last_checked_at"]
            changed_at = row["stable_changed_at"]

            if status == "up":
                online_servers += 1
            elif status == "down":
                offline_servers += 1
            else:
                unknown_servers += 1
            total_servers += 1

            # Calculate uptime/downtime
            uptime_str = ""
            if changed_at:
                duration = now - changed_at
                uptime_str = _format_uptime(duration)

            # Calculate SLA
            sla_up = int(row["sla_up_seconds"] or 0)
            sla_down = int(row["sla_down_seconds"] or 0)
            sla_pct, sla_emoji = _calc_sla(sla_up, sla_down)

            servers.append({
                "ip": row["content"],
                "status": status,
                "last_check": last_check,
                "uptime": uptime_str,
                "sla": sla_pct,
                "sla_emoji": sla_emoji,
            })

        domain_stats.append({
            "hostname": hostname,
            "zone_id": zone_id,
            "enabled": enabled,
            "servers": sorted(servers, key=lambda x: (x["status"] != "up", x["ip"])),
        })

    # Build output
    lines = [
        f"{_e('dashboard', cfg)} <b>Статус системы</b>",
        "",
    ]

    # Summary metrics
    if total_servers > 0:
        health_pct = int((online_servers / total_servers) * 100)
        bar = _progress_bar(online_servers, total_servers, 12)

        lines.append(f"{_e('chart', cfg)} <b>Здоровье:</b> {health_pct}%")
        lines.append(f"    <code>{bar}</code>")
        lines.append("")
        lines.append(
            f"{_e('online', cfg)} <b>{online_servers}</b> онлайн  "
            f"{_e('offline', cfg)} <b>{offline_servers}</b> офлайн  "
            f"{_e('unknown', cfg)} <b>{unknown_servers}</b> н/д"
        )
    else:
        lines.append(f"{_e('info', cfg)} <i>Нет данных о серверах</i>")

    lines.append("")
    lines.append(f"{'─' * 28}")

    # Per-domain details
    for ds in domain_stats:
        status_icon = _e('online', cfg) if ds["enabled"] else _e('offline', cfg)
        enabled_text = "" if ds["enabled"] else " <i>(выкл)</i>"

        lines.append("")
        lines.append(f"{_e('pin', cfg)} <b>{ds['hostname']}</b>{enabled_text}")

        if ds["servers"]:
            for srv in ds["servers"]:
                if srv["status"] == "up":
                    icon = _e('online', cfg)
                    status_text = "онлайн"
                elif srv["status"] == "down":
                    icon = _e('offline', cfg)
                    status_text = "офлайн"
                else:
                    icon = _e('unknown', cfg)
                    status_text = "н/д"

                uptime_info = f" • {srv['uptime']}" if srv["uptime"] else ""

                # SLA info
                sla_info = ""
                if srv["sla"] > 0:
                    sla_icon = _e(srv["sla_emoji"], cfg)
                    sla_info = f" | SLA {sla_icon}{_format_sla(srv['sla'])}"

                lines.append(f"    {icon} <code>{srv['ip']}</code> {status_text}{uptime_info}{sla_info}")
        else:
            lines.append(f"    <i>Нет записей</i>")

    # Footer
    lines.append("")
    lines.append(f"{'─' * 28}")
    lines.append(f"{_e('clock', cfg)} <i>Обновлено: {time.strftime('%H:%M:%S')}</i>")

    return "\n".join(lines)


async def _domain_list_text(conn: aiosqlite.Connection, cfg: Config) -> str:
    """Domain management text."""
    settings = await db_get_domain_settings(conn)
    settings_map = {(d["zone_id"], d["hostname"]): d["enabled"] for d in settings}

    total = len(cfg.zone_hostname_pairs)
    enabled_count = sum(1 for z, h in cfg.zone_hostname_pairs if settings_map.get((z, h), True))

    lines = [
        f"{_e('settings', cfg)} <b>Управление доменами</b>",
        "",
        f"{_e('globe', cfg)} Всего: <b>{total}</b>  |  "
        f"{_e('online', cfg)} Активных: <b>{enabled_count}</b>",
        "",
    ]

    for zone_id, hostname in cfg.zone_hostname_pairs:
        enabled = settings_map.get((zone_id, hostname), True)
        icon = _e('online', cfg) if enabled else _e('offline', cfg)
        lines.append(f"{icon} <code>{hostname}</code>")

    # Domains from DB not in config
    db_only = [d for d in settings if (d["zone_id"], d["hostname"]) not in
               [(z, h) for z, h in cfg.zone_hostname_pairs]]
    if db_only:
        lines.append("")
        lines.append(f"<i>{_e('settings', cfg)} Из базы данных:</i>")
        for d in db_only:
            icon = _e('online', cfg) if d["enabled"] else _e('offline', cfg)
            lines.append(f"{icon} <code>{d['hostname']}</code>")

    lines.append("")
    lines.append(f"<i>Нажмите на домен для вкл/выкл</i>")

    return "\n".join(lines)


# ────────────────────── Handlers ──────────────────────

async def _handle_start(cfg: Config, session: aiohttp.ClientSession, chat_id: int):
    """Handle /start command."""
    await tg_send_with_keyboard(
        cfg, session, _main_menu_text(cfg),
        keyboard=_kb_main_menu(cfg),
    )


async def _handle_status(
    cfg: Config, session: aiohttp.ClientSession,
    conn: aiosqlite.Connection, chat_id: int,
    message_id: Optional[int] = None,
):
    """Handle status display."""
    text = await _status_text(conn, cfg)
    kb = _kb_status(cfg)

    if message_id:
        await tg_edit_message(cfg, session, chat_id, message_id, text, keyboard=kb)
    else:
        await tg_send_with_keyboard(cfg, session, text, keyboard=kb)


async def _handle_callback(
    cfg: Config, session: aiohttp.ClientSession,
    conn: aiosqlite.Connection, callback: dict,
):
    """Dispatch callback_query from inline button press."""
    cb_id = callback["id"]
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    if not chat_id or not message_id:
        await tg_answer_callback(cfg, session, cb_id, f"{EMOJI['cross']} Ошибка")
        return

    parts = data.split(":")
    action = parts[0]

    # Main menu
    if action == "menu" and len(parts) > 1:
        submenu = parts[1]
        if submenu == "main":
            await tg_edit_message(
                cfg, session, chat_id, message_id,
                _main_menu_text(cfg), keyboard=_kb_main_menu(cfg),
            )
        elif submenu == "domains":
            text = await _domain_list_text(conn, cfg)
            kb = await _kb_domain_list(conn, cfg)
            await tg_edit_message(cfg, session, chat_id, message_id, text, keyboard=kb)
        await tg_answer_callback(cfg, session, cb_id)

    # Status
    elif action == "status":
        await _handle_status(cfg, session, conn, chat_id, message_id)
        await tg_answer_callback(cfg, session, cb_id, f"{EMOJI['refresh']} Обновлено")

    # Refresh (same as status but from main menu)
    elif action == "refresh":
        await _handle_status(cfg, session, conn, chat_id, message_id)
        await tg_answer_callback(cfg, session, cb_id, f"{EMOJI['check']} Статус обновлён")

    # Toggle domain
    elif action == "toggle" and len(parts) == 3:
        zone_id, hostname = parts[1], parts[2]
        settings = await db_get_domain_settings(conn)
        current = True
        for d in settings:
            if d["zone_id"] == zone_id and d["hostname"] == hostname:
                current = d["enabled"]
                break

        new_state = not current
        await db_set_domain_enabled(conn, zone_id, hostname, new_state)

        icon = EMOJI["online"] if new_state else EMOJI["offline"]
        state_text = "включён" if new_state else "отключён"
        await tg_answer_callback(cfg, session, cb_id, f"{icon} {hostname} {state_text}")

        text = await _domain_list_text(conn, cfg)
        kb = await _kb_domain_list(conn, cfg)
        await tg_edit_message(cfg, session, chat_id, message_id, text, keyboard=kb)

    # Delete confirmation
    elif action == "delete" and len(parts) == 3:
        zone_id, hostname = parts[1], parts[2]
        text = (
            f"{_e('delete', cfg)} <b>Удалить домен?</b>\n"
            f"\n"
            f"{_e('pin', cfg)} <code>{hostname}</code>\n"
            f"{_e('zone', cfg)} <code>{zone_id[:12]}...</code>\n"
            f"\n"
            f"{_e('warning', cfg)} Домен будет снят с мониторинга\n"
            f"{_e('info', cfg)} DNS записи в CF останутся\n"
        )
        await tg_edit_message(
            cfg, session, chat_id, message_id, text,
            keyboard=_kb_confirm_delete(zone_id, hostname, cfg),
        )
        await tg_answer_callback(cfg, session, cb_id)

    # Confirm delete
    elif action == "confirm_delete" and len(parts) == 3:
        zone_id, hostname = parts[1], parts[2]
        await db_remove_domain(conn, zone_id, hostname)
        cfg.zone_hostname_pairs = [
            (z, h) for z, h in cfg.zone_hostname_pairs
            if not (z == zone_id and h == hostname)
        ]
        await tg_answer_callback(cfg, session, cb_id, f"{EMOJI['delete']} {hostname} удалён")

        text = await _domain_list_text(conn, cfg)
        kb = await _kb_domain_list(conn, cfg)
        await tg_edit_message(cfg, session, chat_id, message_id, text, keyboard=kb)

    # Add domain
    elif action == "add_domain":
        _waiting_for_domain.add(chat_id)
        text = (
            f"{_e('add', cfg)} <b>Добавить домен</b>\n"
            f"\n"
            f"Отправьте в формате:\n"
            f"<code>zone_id:hostname</code>\n"
            f"\n"
            f"<i>Пример:</i>\n"
            f"<code>abc123def456:example.com</code>\n"
        )
        await tg_edit_message(
            cfg, session, chat_id, message_id, text,
            keyboard=_kb_add_domain(cfg),
        )
        await tg_answer_callback(cfg, session, cb_id)

    else:
        await tg_answer_callback(cfg, session, cb_id, f"{EMOJI['warning']} Неизвестная команда")


async def _handle_text_message(
    cfg: Config, session: aiohttp.ClientSession,
    conn: aiosqlite.Connection, message: dict,
):
    """Handle incoming text messages."""
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    # Security: only allowed chat
    if str(chat_id) != str(cfg.tg_chat_id):
        return

    # Commands
    if text.startswith("/start"):
        _waiting_for_domain.discard(chat_id)
        await _handle_start(cfg, session, chat_id)
        return

    if text.startswith("/status"):
        _waiting_for_domain.discard(chat_id)
        await _handle_status(cfg, session, conn, chat_id)
        return

    if text.startswith("/cancel"):
        _waiting_for_domain.discard(chat_id)
        await tg_send_with_keyboard(
            cfg, session, _main_menu_text(cfg),
            keyboard=_kb_main_menu(cfg),
        )
        return

    # Domain input mode
    if chat_id in _waiting_for_domain:
        _waiting_for_domain.discard(chat_id)
        match = re.match(r"^([^:]+):(.+)$", text)
        if not match:
            await tg_send_with_keyboard(
                cfg, session,
                f"{_e('cross', cfg)} <b>Неверный формат</b>\n\n"
                f"Используйте: <code>zone_id:hostname</code>",
                keyboard=_kb_main_menu(cfg),
            )
            return

        zone_id = match.group(1).strip()
        hostname = match.group(2).strip()

        # Show loading
        await tg_send_with_keyboard(
            cfg, session,
            f"{_e('refresh', cfg)} <b>Проверяю зону...</b>\n\n"
            f"{_e('zone', cfg)} <code>{zone_id[:20]}...</code>\n"
            f"{_e('pin', cfg)} <code>{hostname}</code>",
            keyboard=[],
        )

        success, msg, ips = await _validate_and_sync_domain(
            session, conn, cfg, zone_id, hostname
        )

        if not success:
            await tg_send_with_keyboard(
                cfg, session,
                f"{msg}\n\n"
                f"{_e('zone', cfg)} <code>{zone_id}</code>\n"
                f"{_e('pin', cfg)} <code>{hostname}</code>\n\n"
                f"<i>Проверьте zone_id и права API токена</i>",
                keyboard=_kb_main_menu(cfg),
            )
            return

        # Success (force=True to override soft-delete)
        await db_add_domain(conn, zone_id, hostname, force=True)
        if (zone_id, hostname) not in cfg.zone_hostname_pairs:
            cfg.zone_hostname_pairs.append((zone_id, hostname))

        lines = [
            f"{_e('check', cfg)} <b>Домен добавлен!</b>",
            "",
            f"{_e('pin', cfg)} <code>{hostname}</code>",
            f"{_e('zone', cfg)} <code>{zone_id[:20]}...</code>",
            "",
            msg,
        ]

        if ips:
            lines.append("")
            lines.append(f"{_e('dns', cfg)} <b>DNS записи:</b>")
            for ip in ips[:8]:
                lines.append(f"    {_e('server', cfg)} <code>{ip}</code>")
            if len(ips) > 8:
                lines.append(f"    <i>...и ещё {len(ips) - 8}</i>")

        await tg_send_with_keyboard(
            cfg, session,
            "\n".join(lines),
            keyboard=_kb_main_menu(cfg),
        )


# ────────────────────── Polling Loop ──────────────────────

async def tg_polling_loop(
    cfg: Config, session: aiohttp.ClientSession,
    conn: aiosqlite.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Long-poll Telegram getUpdates and dispatch to handlers."""
    if not cfg.tg_enabled:
        info("Telegram polling disabled (tg_enabled=False)")
        return

    info("🤖 Telegram polling started")
    url = f"https://api.telegram.org/bot{cfg.tg_token}/getUpdates"
    offset = 0
    poll_timeout = 30

    while not shutdown_event.is_set():
        try:
            params = {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": '["message","callback_query"]',
            }
            async with session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=poll_timeout + 10),
            ) as resp:
                data = await resp.json()

            if not data.get("ok"):
                warn(f"getUpdates error: {data}")
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                try:
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_chat = cb.get("message", {}).get("chat", {}).get("id")
                        if str(cb_chat) == str(cfg.tg_chat_id):
                            await _handle_callback(cfg, session, conn, cb)
                    elif "message" in update:
                        await _handle_text_message(cfg, session, conn, update["message"])
                except Exception as e:
                    warn(f"Error handling update: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            warn(f"Polling error: {e}")
            await asyncio.sleep(5)

    info("🤖 Telegram polling stopped")
