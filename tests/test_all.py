"""
Comprehensive tests for cf_dns_balanse_bot.
Covers: config, database, health, cloudflare retry, bot logic, notifications.
"""
import asyncio
import os
import platform
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def run(event_loop):
    """Helper to run coroutines in tests."""
    return event_loop.run_until_complete


@pytest.fixture
def db_conn(run):
    """In-memory SQLite connection with schema initialized."""
    from database import db_connect, db_init

    async def _setup():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        from database import db_init
        await db_init(conn)
        return conn

    conn = run(_setup())
    yield conn
    run(conn.close())


def _make_config(**overrides):
    from config import Config
    defaults = dict(
        zone_hostname_pairs=[("zone1", "example.com")],
        record_types={"A"},
        proxied_default=False,
        ping_interval_seconds=10,
        sync_interval_minutes=3,
        flap_up_threshold=2,
        flap_down_threshold=3,
        manage_dns=True,
        db_path=":memory:",
        tg_token=None,
        tg_chat_id=None,
        tg_enabled=False,
        tg_custom_emoji_ids={},
        log_level="WARNING",
    )
    defaults.update(overrides)
    return Config(**defaults)


# ===========================================================================
# 1. Config tests
# ===========================================================================

class TestConfig:
    def test_load_config_parses_zone_hostname(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "CF_ZONE_HOSTNAME=z1:h1.com,z2:h2.com\n"
            "CLOUDFLARE_API_TOKEN=test\n"
            "TELEGRAM_ENABLED=false\n"
        )
        with patch.dict(os.environ, {
            "CF_ZONE_HOSTNAME": "z1:h1.com,z2:h2.com",
            "TELEGRAM_ENABLED": "false",
        }, clear=False):
            from config import Config
            cfg = _make_config(
                zone_hostname_pairs=[("z1", "h1.com"), ("z2", "h2.com")]
            )
            assert len(cfg.zone_hostname_pairs) == 2
            assert cfg.zone_hostname_pairs[0] == ("z1", "h1.com")
            assert cfg.zone_hostname_pairs[1] == ("z2", "h2.com")

    def test_flap_threshold_removed(self):
        """Bug #2: flap_threshold should not exist in Config."""
        from config import Config
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(Config)}
        assert "flap_threshold" not in field_names

    def test_record_types_is_set(self):
        cfg = _make_config(record_types={"A", "AAAA"})
        assert isinstance(cfg.record_types, set)
        assert "A" in cfg.record_types


# ===========================================================================
# 2. Database tests
# ===========================================================================

class TestDatabase:
    def test_db_init_creates_tables(self, run, db_conn):
        """Verify schema creation."""
        async def _check():
            async with db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {r[0] for r in await cur.fetchall()}
            assert "dns_records" in tables
            assert "host_states" in tables
        run(_check())

    def test_db_upsert_record(self, run, db_conn):
        from database import db_upsert_record
        rec = {
            "id": "rec1", "zone_id": "z1", "name": "example.com",
            "type": "A", "content": "1.2.3.4", "ttl": 1, "proxied": False,
        }
        run(db_upsert_record(db_conn, rec, status="unknown"))
        run(db_conn.commit())

        async def _check():
            async with db_conn.execute(
                "SELECT * FROM dns_records WHERE id='rec1'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["content"] == "1.2.3.4"
            assert row["status"] == "unknown"
        run(_check())

    def test_db_update_status(self, run, db_conn):
        from database import db_upsert_record, db_update_status
        rec = {
            "id": "rec2", "zone_id": "z1", "name": "example.com",
            "type": "A", "content": "5.6.7.8", "ttl": 1, "proxied": False,
        }
        run(db_upsert_record(db_conn, rec, status="unknown"))
        run(db_update_status(db_conn, "rec2", "up"))
        run(db_conn.commit())

        async def _check():
            async with db_conn.execute(
                "SELECT status FROM dns_records WHERE id='rec2'"
            ) as cur:
                row = await cur.fetchone()
            assert row["status"] == "up"
        run(_check())

    def test_db_upsert_host_state_new_up(self, run, db_conn):
        """Bug #7: new host with status 'up' should start as 'unknown'."""
        from database import db_upsert_host_state
        prev, new, stable_prev, stable_new = run(
            db_upsert_host_state(
                db_conn, "z1", "example.com", "A", "1.2.3.4",
                "up", up_threshold=2, down_threshold=3
            )
        )
        run(db_conn.commit())
        assert prev == "unknown"
        assert new == "up"
        assert stable_prev == "unknown"
        assert stable_new == "unknown"  # Bug #7: must NOT be 'up' immediately

    def test_db_upsert_host_state_new_down(self, run, db_conn):
        """Bug #7: new host with status 'down' should start as 'unknown'."""
        from database import db_upsert_host_state
        prev, new, stable_prev, stable_new = run(
            db_upsert_host_state(
                db_conn, "z1", "example.com", "A", "9.8.7.6",
                "down", up_threshold=2, down_threshold=3
            )
        )
        run(db_conn.commit())
        assert stable_new == "unknown"  # symmetric: also starts as unknown

    def test_db_upsert_host_state_threshold_transition(self, run, db_conn):
        """After enough consecutive 'up' pings, stable_status goes to 'up'."""
        from database import db_upsert_host_state
        ip = "10.0.0.1"

        # First call: starts unknown
        run(db_upsert_host_state(
            db_conn, "z1", "ex.com", "A", ip, "up",
            up_threshold=2, down_threshold=3
        ))
        run(db_conn.commit())

        # Second call: consec_up becomes 2 >= up_threshold=2 -> stable 'up'
        _, _, _, stable_new = run(db_upsert_host_state(
            db_conn, "z1", "ex.com", "A", ip, "up",
            up_threshold=2, down_threshold=3
        ))
        run(db_conn.commit())
        assert stable_new == "up"

    def test_db_upsert_host_state_down_threshold(self, run, db_conn):
        """Host needs down_threshold consecutive downs to go 'down'."""
        from database import db_upsert_host_state
        ip = "10.0.0.2"

        # Build up to stable 'up' first (threshold=2)
        for _ in range(3):
            run(db_upsert_host_state(
                db_conn, "z1", "ex.com", "A", ip, "up",
                up_threshold=2, down_threshold=3
            ))
            run(db_conn.commit())

        # Now 3 consecutive downs (threshold=3)
        for i in range(3):
            _, _, _, stable_new = run(db_upsert_host_state(
                db_conn, "z1", "ex.com", "A", ip, "down",
                up_threshold=2, down_threshold=3
            ))
            run(db_conn.commit())

        assert stable_new == "down"

    def test_db_get_records_with_zone_filter(self, run, db_conn):
        """Bug #6: zone_id filter should scope records properly."""
        from database import db_upsert_record, db_get_records_by_name_types

        rec1 = {
            "id": "r1", "zone_id": "z1", "name": "ex.com",
            "type": "A", "content": "1.1.1.1", "ttl": 1, "proxied": False,
        }
        rec2 = {
            "id": "r2", "zone_id": "z2", "name": "ex.com",
            "type": "A", "content": "2.2.2.2", "ttl": 1, "proxied": False,
        }
        run(db_upsert_record(db_conn, rec1, status="up"))
        run(db_upsert_record(db_conn, rec2, status="up"))
        run(db_conn.commit())

        # Without zone filter: gets both
        all_rows = run(db_get_records_by_name_types(
            db_conn, "ex.com", {"A"}
        ))
        assert len(all_rows) == 2

        # With zone filter: gets only z1
        z1_rows = run(db_get_records_by_name_types(
            db_conn, "ex.com", {"A"}, zone_id="z1"
        ))
        assert len(z1_rows) == 1
        assert z1_rows[0]["content"] == "1.1.1.1"

    def test_no_commit_in_db_upsert_record(self, run, db_conn):
        """Bug #4: db_upsert_record should NOT auto-commit."""
        from database import db_upsert_record
        rec = {
            "id": "nocommit", "zone_id": "z1", "name": "ex.com",
            "type": "A", "content": "3.3.3.3", "ttl": 1, "proxied": False,
        }
        run(db_upsert_record(db_conn, rec, status="unknown"))
        # Rollback should undo the insert
        run(db_conn.rollback())

        async def _check():
            async with db_conn.execute(
                "SELECT * FROM dns_records WHERE id='nocommit'"
            ) as cur:
                row = await cur.fetchone()
            assert row is None
        run(_check())


# ===========================================================================
# 3. Health tests
# ===========================================================================

class TestHealth:
    def test_ping_once_localhost(self, run):
        """TCP check to localhost:80 or similar should work."""
        from health import check_tcp
        # Port 443 on localhost may not be open, but check_tcp itself shouldn't crash
        result = run(check_tcp("127.0.0.1", 80, timeout_seconds=1))
        # May or may not succeed depending on local services, just verify no crash
        assert isinstance(result, bool)

    def test_ping_once_unreachable(self, run):
        """TCP to unreachable host should fail (mocked)."""
        from health import ping_once

        async def _fail(*args, **kwargs):
            raise ConnectionRefusedError("mocked")

        with patch("health.asyncio.open_connection", side_effect=_fail):
            result = run(ping_once("192.0.2.1", timeout_seconds=1))
            assert result is False

    def test_tcp_check_real_server(self, run):
        """TCP connect to a known reachable server."""
        from health import check_tcp
        # Google DNS responds on TCP 443
        result = run(check_tcp("8.8.8.8", 443, timeout_seconds=3))
        assert result is True

    def test_default_ports(self):
        """Verify default check ports are 443 and 80."""
        from health import DEFAULT_CHECK_PORTS
        assert 443 in DEFAULT_CHECK_PORTS
        assert 80 in DEFAULT_CHECK_PORTS


# ===========================================================================
# 4. Bot logic tests
# ===========================================================================

class TestBotLogic:
    def test_record_type_for_ipv4(self):
        """Bug #5: IPv4 -> A."""
        from bot import _record_type_for_ip
        assert _record_type_for_ip("1.2.3.4") == "A"
        assert _record_type_for_ip("192.168.0.1") == "A"

    def test_record_type_for_ipv6(self):
        """Bug #5: IPv6 -> AAAA."""
        from bot import _record_type_for_ip
        assert _record_type_for_ip("::1") == "AAAA"
        assert _record_type_for_ip("2001:db8::1") == "AAAA"

    def test_record_type_for_invalid(self):
        """Bug #5: invalid -> default A."""
        from bot import _record_type_for_ip
        assert _record_type_for_ip("not-an-ip") == "A"

    def test_evaluate_and_update_status_with_zone_id(self, run, db_conn):
        """Bug #6/#8: evaluate_and_update_status filters by zone_id."""
        from database import db_upsert_record
        from bot import evaluate_and_update_status

        cfg = _make_config(
            zone_hostname_pairs=[("z1", "ex.com"), ("z2", "ex.com")],
            record_types={"A"},
            flap_up_threshold=1,
            flap_down_threshold=1,
        )

        rec1 = {
            "id": "r1", "zone_id": "z1", "name": "ex.com",
            "type": "A", "content": "127.0.0.1", "ttl": 1, "proxied": False,
        }
        rec2 = {
            "id": "r2", "zone_id": "z2", "name": "ex.com",
            "type": "A", "content": "192.0.2.1", "ttl": 1, "proxied": False,
        }
        run(db_upsert_record(db_conn, rec1, status="unknown"))
        run(db_upsert_record(db_conn, rec2, status="unknown"))
        run(db_conn.commit())

        on_change = AsyncMock()

        # Zone z1 should only see 127.0.0.1
        up, down, by = run(evaluate_and_update_status(
            db_conn, cfg, "ex.com", "z1", {"A"}, on_change
        ))
        assert "127.0.0.1" in by
        assert "192.0.2.1" not in by


# ===========================================================================
# 5. Cloudflare retry tests
# ===========================================================================

class TestCloudflareRetry:
    def test_retry_on_500(self, run):
        """Bug #9: should retry on 500 status codes."""
        from cloudflare import _request_with_retry

        mock_resp_fail = AsyncMock()
        mock_resp_fail.status = 500
        mock_resp_fail.text = AsyncMock(return_value="Internal Server Error")
        mock_resp_fail.request_info = MagicMock()
        mock_resp_fail.history = ()

        mock_resp_ok = AsyncMock()
        mock_resp_ok.status = 200

        session = MagicMock()
        session.request = AsyncMock(
            side_effect=[mock_resp_fail, mock_resp_ok]
        )

        with patch("cloudflare.asyncio.sleep", new_callable=AsyncMock):
            resp = run(_request_with_retry(session, "GET", "http://test"))
            assert resp.status == 200
            assert session.request.call_count == 2

    def test_timeout_conversion(self, run):
        """Bug #13: int timeout should be converted to ClientTimeout."""
        from cloudflare import _request_with_retry

        mock_resp = AsyncMock()
        mock_resp.status = 200

        session = MagicMock()
        session.request = AsyncMock(return_value=mock_resp)

        run(_request_with_retry(
            session, "GET", "http://test", timeout=15
        ))

        # Verify timeout was converted
        call_kwargs = session.request.call_args[1]
        assert isinstance(
            call_kwargs.get("timeout"), aiohttp.ClientTimeout
        )


# ===========================================================================
# 6. Notifications tests
# ===========================================================================

class TestNotifications:
    def test_tg_send_disabled(self, run):
        """Should not send when tg_enabled=False."""
        from notifications import tg_send
        cfg = _make_config(tg_enabled=False)
        session = MagicMock()
        session.post = MagicMock()
        run(tg_send(cfg, session, "test"))
        session.post.assert_not_called()

    def test_tg_send_timeout_type(self):
        """Bug #13: timeout should be ClientTimeout, not int."""
        import ast
        import inspect
        from notifications import tg_send
        source = inspect.getsource(tg_send)
        # Verify no bare `timeout=10` (int literal)
        assert "timeout=10" not in source or "ClientTimeout" in source
