"""Shared pytest fixtures for the cloudformation test suite."""

import os
import pytest


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def duplo():
    """A DuploCtl instance pointed at the live test portal."""
    pytest.importorskip("duploctl_aws")
    from duplocloud.controller import DuploCtl
    host = os.environ.get("DUPLO_HOST")
    token = os.environ.get("DUPLO_TOKEN")
    if not host or not token:
        pytest.skip("DUPLO_HOST / DUPLO_TOKEN not set")
    duplo = DuploCtl(host=host, token=token, tenant="default")
    duplo.isadmin = True
    # Disable credential cache so JIT always fetches fresh STS tokens.
    # Without this, cached tokens expire mid-test causing ClientError:
    # ExpiredToken on long-running test runs.
    duplo.nocache = True
    return duplo


@pytest.fixture(scope="session")
def aws_plugin(duplo):
    """The duploctl-aws plugin, providing boto3 client helpers."""
    return duplo.load("aws")


@pytest.fixture(scope="function")
def cfn_boto(aws_plugin):
    """A boto3 CloudFormation client authenticated via DuploCloud JIT.

    Function-scoped so each test gets a fresh boto3 client with current
    STS credentials, preventing ExpiredToken errors on long test runs.
    """
    return aws_plugin.load("cloudformation", refresh=True)


@pytest.fixture(scope="session")
def ecr_boto(aws_plugin):
    """A boto3 ECR client authenticated via DuploCloud JIT."""
    return aws_plugin.load("ecr")


@pytest.fixture(scope="session")
def sts_boto(aws_plugin):
    """A boto3 STS client for account/region discovery."""
    return aws_plugin.load("sts")


@pytest.fixture(scope="session")
def aws_account_id(sts_boto):
    """The AWS account ID for the test environment."""
    return sts_boto.get_caller_identity()["Account"]


@pytest.fixture(scope="session")
def aws_region(ecr_boto):
    """The AWS region for the test environment."""
    return ecr_boto.meta.region_name


@pytest.fixture()
def cfn_event_base():
    """Minimal CloudFormation custom resource event skeleton."""
    return {
        "ResponseURL": "https://s3.amazonaws.com/bucket/key?signature=abc",
        "StackId": "arn:aws:cloudformation:us-east-1:123:stack/MyStack/abc",
        "RequestId": "req-1234",
        "LogicalResourceId": "MyService",
        "PhysicalResourceId": "service::my-tenant::nginx",
        "ResourceType": "Custom::Duplo@Service",
    }


@pytest.fixture()
def create_event(cfn_event_base):
    """CFN Create event."""
    return {
        **cfn_event_base,
        "RequestType": "Create",
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:123:function:duploctl",
            "Tenant": "my-tenant",
            "Body": {"Name": "nginx", "Image": "nginx:latest"},
        },
    }


@pytest.fixture()
def update_event(cfn_event_base):
    """CFN Update event."""
    return {
        **cfn_event_base,
        "RequestType": "Update",
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:123:function:duploctl",
            "Tenant": "my-tenant",
            "Body": {"Name": "nginx", "Image": "nginx:1.25"},
        },
    }


@pytest.fixture()
def delete_event(cfn_event_base):
    """CFN Delete event."""
    return {
        **cfn_event_base,
        "RequestType": "Delete",
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:123:function:duploctl",
            "Tenant": "my-tenant",
            "Body": {"Name": "nginx"},
        },
    }
