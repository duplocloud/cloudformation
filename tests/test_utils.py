"""Unit tests for duplocloud.cfn.utils."""

import pytest

from duplocloud.cfn.utils import extract_properties, get_id, parse_resource_type


class TestParseResourceType:
    def test_basic(self):
        assert parse_resource_type("Custom::Duplo@Service") == "service"

    def test_uppercase_preserved_lowercased(self):
        assert parse_resource_type("Custom::Duplo@S3") == "s3"

    def test_underscore(self):
        assert parse_resource_type("Custom::Duplo@Batch_Compute") == "batch_compute"

    def test_already_lowercase(self):
        assert parse_resource_type("Custom::Duplo@tenant") == "tenant"

    def test_missing_at_raises(self):
        with pytest.raises(ValueError, match="Invalid ResourceType"):
            parse_resource_type("Custom::DuploService")


class TestExtractProperties:
    def test_body_key_used_directly(self):
        props = {
            "ServiceToken": "arn:...",
            "Tenant": "my-tenant",
            "Body": {"Name": "nginx"},
        }
        reserved, body = extract_properties(props)
        assert body == {"Name": "nginx"}
        assert reserved["Tenant"] == "my-tenant"
        assert "Body" not in reserved

    def test_flat_style_body(self):
        props = {
            "ServiceToken": "arn:...",
            "AccountName": "my-tenant",
            "PlanID": "my-infra",
        }
        reserved, body = extract_properties(props)
        assert body == {"AccountName": "my-tenant", "PlanID": "my-infra"}

    def test_bool_coercion_false_string(self):
        props = {"Wait": "false", "Validate": "False", "AllowImport": "0"}
        reserved, _ = extract_properties(props)
        assert reserved["Wait"] is False
        assert reserved["Validate"] is False
        assert reserved["AllowImport"] is False

    def test_bool_coercion_true_string(self):
        props = {"Wait": "true", "Validate": "True", "AllowImport": "yes"}
        reserved, _ = extract_properties(props)
        assert reserved["Wait"] is True
        assert reserved["Validate"] is True
        assert reserved["AllowImport"] is True

    def test_service_token_stripped(self):
        props = {"ServiceToken": "arn:...", "ServiceTimeout": "300"}
        reserved, body = extract_properties(props)
        assert "ServiceToken" not in body
        assert "ServiceTimeout" not in body

    def test_query_preserved(self):
        props = {"Query": "[].Name"}
        reserved, _ = extract_properties(props)
        assert reserved["Query"] == "[].Name"

    def test_empty_props(self):
        reserved, body = extract_properties({})
        assert reserved == {}
        assert body == {}

    def test_none_props(self):
        reserved, body = extract_properties(None)
        assert reserved == {}
        assert body == {}


class _FakeResource:
    """Minimal stand-in for a duploctl resource object."""

    def __init__(self, slug, tenant=None):
        self.slug = slug
        self._tenant = tenant

    def name_from_body(self, data):
        return data.get("Name") or data.get("metadata", {}).get("name")


class TestGetId:
    def test_portal_scoped(self):
        res = _FakeResource("tenant")
        assert get_id(res, {"Name": "my-tenant"}) == "tenant::my-tenant"

    def test_tenant_scoped_string(self):
        res = _FakeResource("service", tenant="dev01")
        assert get_id(res, {"Name": "nginx"}) == "service::dev01::nginx"

    def test_tenant_scoped_dict(self):
        res = _FakeResource("service", tenant={"AccountName": "dev01"})
        assert get_id(res, {"Name": "nginx"}) == "service::dev01::nginx"

    def test_v3_metadata_name(self):
        res = _FakeResource("configmap")
        data = {"metadata": {"name": "my-config"}}
        assert get_id(res, data) == "configmap::my-config"
