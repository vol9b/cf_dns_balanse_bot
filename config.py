import os
import re
from dataclasses import dataclass
from typing import List, Set, Tuple, Optional, Dict
from dotenv import load_dotenv


@dataclass
class Config:
    zone_hostname_pairs: List[Tuple[str, str]]  # (zone_id, hostname)
    record_types: Set[str]
    proxied_default: bool
    ping_interval_seconds: int
    sync_interval_minutes: int
    flap_up_threshold: int
    flap_down_threshold: int
    manage_dns: bool
    db_path: str
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    tg_enabled: bool
    tg_custom_emoji_ids: Dict[str, str]
    log_level: str = "INFO"


def load_config_from_env() -> Config:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=env_path, override=True)

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
    flap_up_threshold = int(os.getenv("FLAP_UP_THRESHOLD", "2"))
    flap_down_threshold = int(os.getenv("FLAP_DOWN_THRESHOLD", "3"))
    manage_dns = (os.getenv("CF_MANAGE_DNS", "true").strip().lower() in {"1", "true", "yes", "on"})

    db_path = os.getenv("CF_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cf_dns.db")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    tg_enabled = (os.getenv("TELEGRAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}) and bool(tg_token and tg_chat_id)

    # Format: key=id,key2=id2
    tg_custom_emoji_raw = os.getenv("TELEGRAM_PREMIUM_EMOJI_IDS", "")
    tg_custom_emoji_ids = {}
    if tg_custom_emoji_raw:
        for pair in tg_custom_emoji_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                tg_custom_emoji_ids[k.strip()] = v.strip()

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    return Config(
        zone_hostname_pairs=zone_hostname_pairs,
        record_types=record_types,
        proxied_default=proxied_default,
        ping_interval_seconds=ping_interval_seconds,
        sync_interval_minutes=sync_interval_minutes,

        flap_up_threshold=flap_up_threshold,
        flap_down_threshold=flap_down_threshold,
        manage_dns=manage_dns,
        db_path=db_path,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        tg_enabled=tg_enabled,
        tg_custom_emoji_ids=tg_custom_emoji_ids,
        log_level=log_level,
    )
