"""Unit tests for the Lambda handler event routing."""

import pytest

from cfn_lambda.cfn import is_cfn_event
from cfn_lambda.handler import handler


class TestIsCfnEvent:
    def test_cfn_event_detected(self, create_event):
        assert is_cfn_event(create_event) is True

    def test_adhoc_event_not_cfn(self):
        assert is_cfn_event({"kind": "service", "cmd": "list"}) is False

    def test_empty_event_not_cfn(self):
        assert is_cfn_event({}) is False


class TestHandlerRouting:
    def test_adhoc_disabled_raises(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "false")
        with pytest.raises(RuntimeError, match="disabled"):
            handler({"kind": "service", "cmd": "list"}, None)

    def test_adhoc_missing_kind_raises(self, monkeypatch):
        monkeypatch.setenv("DUPLO_ADHOC_ENABLED", "true")
        monkeypatch.setenv("DUPLO_HOST", "https://test.duplocloud.net")
        monkeypatch.setenv("DUPLO_TOKEN", "test-token")
        with pytest.raises((ValueError, KeyError)):
            handler({}, None)
