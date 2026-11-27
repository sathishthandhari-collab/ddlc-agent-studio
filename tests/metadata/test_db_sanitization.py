"""Unit tests for src/metadata/db.py (sanitization, truncation, 64KB guards, and pool lifecycle)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import BaseModel
from src.core.settings import RuntimeEnvSettings, get_env_settings
from src.metadata.db import (
    REDACTED_VALUE,
    MetadataDatabase,
    guard_inline_jsonb,
    sanitize_payload,
    sanitize_text,
    tier1_table,
    tier2_table,
    to_jsonb,
    truncate_stack_trace,
)


class _SampleModel(BaseModel):
    token: str
    count: int


class TestSanitizationAndTruncation:
    def test_sanitize_text_redacts_configured_patterns(self) -> None:
        raw = (
            "Failed call with key sk-proj1234567890abcdef1234 "
            "and header Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig=="
        )
        cleaned = sanitize_text(raw)
        assert "sk-proj1234567890abcdef1234" not in cleaned
        assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
        assert cleaned.count(REDACTED_VALUE) == 2

    def test_sanitize_text_handles_none_and_empty(self) -> None:
        assert sanitize_text(None) is None
        assert sanitize_text("") == ""
        assert sanitize_text("plain log message") == "plain log message"

    def test_sanitize_payload_handles_nested_and_special_types(self) -> None:
        uid = UUID("11111111-2222-3333-4444-555555555555")
        ts = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        circular: dict[str, object] = {"safe": "ok"}
        circular["self"] = circular

        payload = {
            "auth": "Bearer mySecretToken12345",
            "api_key": "sk-testKeyValue9876543210",
            "nested": [
                _SampleModel(token="sk-modelSecret1234567890", count=3),
                (uid, ts, Decimal("1.25"), Path("contracts/tier_1_agent_metadata.yaml")),
                b"bytes-secret sk-bytesSecret1234567890",
            ],
            "circular": circular,
        }
        cleaned = sanitize_payload(payload)
        serialized = json.dumps(cleaned)
        assert "mySecretToken12345" not in serialized
        assert "sk-testKeyValue9876543210" not in serialized
        assert "sk-modelSecret1234567890" not in serialized
        assert "sk-bytesSecret1234567890" not in serialized
        assert str(uid) in serialized
        assert "[CIRCULAR_REF]" in serialized

    def test_truncate_stack_trace_caps_utf8_bytes_safely(self) -> None:
        # Mix 4-byte emoji and multi-byte chars with a leaked secret
        secret = "sk-leakTrace1234567890abcdef"
        unit = "🚨é"  # 4 + 2 = 6 UTF-8 bytes
        trace = f"Error with {secret}\n" + (unit * 1000)
        limit = 200
        truncated = truncate_stack_trace(trace, max_bytes=limit)
        assert truncated is not None
        assert secret not in truncated
        assert REDACTED_VALUE in truncated
        assert truncated.endswith("[TRUNCATED]")
        encoded = truncated.encode("utf-8")
        assert len(encoded) <= limit
        # Ensure clean valid UTF-8 roundtrip
        assert encoded.decode("utf-8") == truncated

    def test_truncate_stack_trace_handles_small_and_zero_limits(self) -> None:
        assert truncate_stack_trace(None) is None
        assert truncate_stack_trace("traceback text", max_bytes=0) == ""
        tiny = truncate_stack_trace("abcdef🚨ghijk", max_bytes=8)
        assert tiny is not None
        assert len(tiny.encode("utf-8")) <= 8

    def test_guard_inline_jsonb_allows_small_and_replaces_oversized(self) -> None:
        small = {"key": "Bearer token_secret_12345", "rows": [1, 2, 3]}
        guarded_small = guard_inline_jsonb(small)
        assert guarded_small == {"key": REDACTED_VALUE, "rows": [1, 2, 3]}

        # Exceed default 65536 bytes
        oversized = {
            "large_blob": "x" * 70000,
            "extra": "sk-secretInsideLargeBlob12345",
        }
        guarded_large = guard_inline_jsonb(
            oversized,
            artifact_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            storage_uri="s3://ddlc-artifacts/runs/1/blob.json",
        )
        assert isinstance(guarded_large, dict)
        assert guarded_large["_truncated"] is True
        assert guarded_large["original_byte_size"] > 65536
        assert guarded_large["max_inline_bytes"] == 65536
        assert guarded_large["artifact_registry_ref"]["table"] == "agent_metadata.artifact_registry"
        assert guarded_large["artifact_registry_ref"]["artifact_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert len(json.dumps(guarded_large).encode("utf-8")) <= 65536

    def test_guard_inline_jsonb_respects_tight_custom_limit(self) -> None:
        big = {"a": "z" * 1000}
        guarded = guard_inline_jsonb(big, max_bytes=120)
        assert guarded["_truncated"] is True
        assert len(json.dumps(guarded).encode("utf-8")) <= 120
        assert guard_inline_jsonb(None) is None
        assert to_jsonb(None) is None


class TestMetadataDatabaseLifecycle:
    def test_schema_table_helpers(self) -> None:
        assert tier1_table("execution_log") == '"agent_metadata"."execution_log"'
        assert tier2_table("agent_registry") == '"ddlc_shared_metadata"."agent_registry"'

    def test_lazy_init_does_not_require_uri_until_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("METADATA_DB_URI", raising=False)
        get_env_settings.cache_clear()
        monkeypatch.setattr(
            "src.metadata.db.get_env_settings",
            lambda: RuntimeEnvSettings(metadata_db_uri=None),
        )

        db = MetadataDatabase()
        with pytest.raises(ValueError, match="METADATA_DB_URI"):
            with db.connection():
                pass

    def test_injected_pool_transaction_commits_and_rolls_back(self) -> None:
        mock_conn = MagicMock()
        mock_tx = MagicMock()
        mock_tx.__enter__.return_value = None
        mock_tx.__exit__.return_value = False
        mock_conn.transaction.return_value = mock_tx

        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_pool.connection.return_value.__exit__.return_value = False

        db = MetadataDatabase(pool=mock_pool)
        with db.transaction() as conn:
            conn.execute("SELECT 1")

        mock_conn.transaction.assert_called_once()
        mock_conn.execute.assert_called_once_with("SELECT 1")

        # Injected pool must not be closed by MetadataDatabase.close()
        db.close()
        mock_pool.close.assert_not_called()
