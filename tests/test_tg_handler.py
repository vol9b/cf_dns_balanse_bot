import pytest
import aiosqlite
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from config import Config
from database import (
    db_init, db_get_domain_settings, db_set_domain_enabled,
    db_add_domain, db_remove_domain
)
from tg_handler import _kb_domain_list, _domain_list_text, EMOJI


# ────────────────────── Fixtures ──────────────────────

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
def mock_config():
    return Config(
        zone_hostname_pairs=[("z1", "h1"), ("z2", "h2")],
        record_types={"A"},
        proxied_default=False,
        ping_interval_seconds=10,
        sync_interval_minutes=3,
        flap_up_threshold=1,
        flap_down_threshold=1,
        manage_dns=True,
        db_path=":memory:",
        tg_token="fake",
        tg_chat_id="123",
        tg_enabled=True,
        tg_custom_emoji_ids={"enabled": "123", "disabled": "456"},
        log_level="DEBUG",
    )


@pytest.fixture
def mem_db(run):
    """In-memory SQLite connection with schema initialized."""
    async def _setup():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await db_init(conn)
        return conn

    conn = run(_setup())
    yield conn
    run(conn.close())


# ────────────────────── Tests ──────────────────────

class TestDatabaseDomainSettings:
    def test_crud_domain_settings(self, run, mem_db):
        """Test CRUD operations for domain settings."""
        # 1. Add domain
        run(db_add_domain(mem_db, "z1", "h1"))
        settings = run(db_get_domain_settings(mem_db))
        assert len(settings) == 1
        assert settings[0]["hostname"] == "h1"
        assert settings[0]["enabled"] is True

        # 2. Toggle disabled
        run(db_set_domain_enabled(mem_db, "z1", "h1", False))
        settings = run(db_get_domain_settings(mem_db))
        assert settings[0]["enabled"] is False

        # 3. Toggle enabled
        run(db_set_domain_enabled(mem_db, "z1", "h1", True))
        settings = run(db_get_domain_settings(mem_db))
        assert settings[0]["enabled"] is True

        # 4. Remove
        run(db_remove_domain(mem_db, "z1", "h1"))
        settings = run(db_get_domain_settings(mem_db))
        assert len(settings) == 0


class TestTgHandlerInternals:
    def test_domain_list_builder(self, run, mem_db, mock_config):
        """Test that domain list text and keyboard generate correctly."""
        # Seed DB
        run(db_set_domain_enabled(mem_db, "z1", "h1", False))
        # h2 not in DB, should default to True

        text = run(_domain_list_text(mem_db, mock_config))
        kb = run(_kb_domain_list(mem_db, mock_config))

        # Check text contains domain info (new format uses emoji from EMOJI dict)
        assert "h1" in text
        assert "h2" in text
        assert EMOJI["offline"] in text  # h1 is disabled
        assert EMOJI["online"] in text   # h2 is enabled

        # Verify keyboard structure
        # h1 row (Disabled -> Red/Danger)
        assert kb[0][0]["text"] == f"{EMOJI['offline']} h1"
        assert kb[0][0]["callback_data"] == "toggle:z1:h1"
        assert kb[0][0]["style"] == "danger"

        # h2 row (Enabled -> Green/Success)
        assert kb[1][0]["text"] == f"{EMOJI['online']} h2"
        assert kb[1][0]["callback_data"] == "toggle:z2:h2"
        assert kb[1][0]["style"] == "success"

    def test_callbacks_dispatch(self, run, mem_db, mock_config):
        """Test dispatching callbacks to handlers."""
        from tg_handler import _handle_callback
        
        session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"ok": True}
        mock_resp.text.return_value = "ok"
        
        # Mock session.post() context manager
        session.post.return_value.__aenter__.return_value = mock_resp

        # 1. Toggle h1 -> disable
        # First ensure it exists (default True)
        run(db_add_domain(mem_db, "z1", "h1"))
        
        cb = {
            "id": "cb1",
            "data": "toggle:z1:h1",
            "message": {
                "message_id": 999,
                "chat": {"id": 123}
            }
        }
        run(_handle_callback(mock_config, session, mem_db, cb))
        
        # Verify DB changed
        settings = run(db_get_domain_settings(mem_db))
        h1 = next(s for s in settings if s["hostname"] == "h1")
        assert h1["enabled"] is False
        
        # Verify calls
        assert session.post.called

    def test_add_domain_flow(self, run, mem_db, mock_config):
        """Test the multi-step add domain flow with CF API validation."""
        import os
        from tg_handler import _handle_text_message, _waiting_for_domain, _handle_callback

        # Set API token for validation
        os.environ["CLOUDFLARE_API_TOKEN"] = "test_token"

        session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"ok": True}
        session.post.return_value.__aenter__.return_value = mock_resp

        # Mock CF API response for cf_list_records (used by _request_with_retry)
        mock_cf_resp = MagicMock()
        mock_cf_resp.status = 200
        mock_cf_resp.raise_for_status = MagicMock()

        async def mock_json():
            return {
                "success": True,
                "result": [
                    {"id": "rec1", "name": "newhost.com", "type": "A",
                     "content": "1.2.3.4", "ttl": 1, "proxied": False}
                ],
                "result_info": {"total_pages": 1}
            }
        mock_cf_resp.json = mock_json

        async def mock_request(*args, **kwargs):
            return mock_cf_resp
        session.request = mock_request

        chat_id = 123

        # 1. Trigger "add_domain" callback
        cb = {
            "id": "cb2",
            "data": "add_domain",
            "message": {"message_id": 888, "chat": {"id": chat_id}}
        }
        run(_handle_callback(mock_config, session, mem_db, cb))
        assert chat_id in _waiting_for_domain

        # 2. Send text "zone:host"
        msg = {
            "chat": {"id": chat_id},
            "text": "newzone:newhost.com"
        }
        run(_handle_text_message(mock_config, session, mem_db, msg))

        assert chat_id not in _waiting_for_domain

        # Verify added to DB
        settings = run(db_get_domain_settings(mem_db))
        assert any(d["hostname"] == "newhost.com" for d in settings)

        # Verify added to config memory
        assert ("newzone", "newhost.com") in mock_config.zone_hostname_pairs

        # Cleanup
        del os.environ["CLOUDFLARE_API_TOKEN"]
