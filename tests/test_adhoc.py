"""Unit tests for duplocloud.cfn.adhoc."""

import pytest

from duplocloud.cfn.adhoc import handle_adhoc_event, is_adhoc_enabled


class TestIsAdhocEnabled:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("DUPLO_ADHOC_ENABLED", raising=False)
        assert is_adhoc_enabled() is True

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "true")
        assert is_adhoc_enabled() is True

    def test_explicit_false(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "false")
        assert is_adhoc_enabled() is False

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "0")
        assert is_adhoc_enabled() is False

    def test_no_disables(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "no")
        assert is_adhoc_enabled() is False


class TestHandleAdhocEvent:
    def test_disabled_raises(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "false")
        with pytest.raises(RuntimeError, match="disabled"):
            handle_adhoc_event({"kind": "service"})

    def test_missing_kind_raises(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "true")
        monkeypatch.setenv("DUPLO_HOST", "https://test.duplocloud.net")
        monkeypatch.setenv("DUPLO_TOKEN", "test-token")
        with pytest.raises(ValueError, match="kind"):
            handle_adhoc_event({})
