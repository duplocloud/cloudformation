"""Shared pytest fixtures for the cloudformation test suite."""

import pytest


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
