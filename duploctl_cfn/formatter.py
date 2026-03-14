"""CFN output formatter for duplocloud.

Registered as 'cfn' under the formats.duplocloud.net entry point.
Converts duploctl resource data to a CloudFormation
Custom::Duplo@<Kind> YAML fragment.

Usage:
    duploctl service find myservice -o cfn
    duploctl tenant find mytenant -o cfn
"""

import yaml


class _Sub(str):
    """YAML tag helper that emits CloudFormation !Sub intrinsics."""


def _sub_representer(dumper, data):
    return dumper.represent_scalar("!Sub", str(data))


yaml.add_representer(_Sub, _sub_representer)

_DEFAULT_SERVICE_TOKEN = _Sub(
    "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}"
    ":function:duploservices-default-duploctl-cfn"
)


def tocfn(obj) -> str:
    """Format a duploctl resource as a CloudFormation custom resource.

    Wraps resource data in a Custom::Duplo@<Kind> resource block
    suitable for inclusion under the 'Resources' key of a
    CloudFormation template. The ServiceToken is automatically
    set to the default DuploCloud CFN lambda ARN using !Sub.

    Args:
      obj: A dict (single resource) or list of dicts.

    Returns:
      A YAML string with CloudFormation resource definition(s).
    """
    if isinstance(obj, list):
        result = {}
        for item in obj:
            logical, block = _to_cfn_resource(item)
            result[logical] = block
        return yaml.dump(
            result, default_flow_style=False, sort_keys=False
        )
    logical, block = _to_cfn_resource(obj)
    return yaml.dump(
        {logical: block},
        default_flow_style=False,
        sort_keys=False,
    )


def _to_cfn_resource(data: dict) -> tuple:
    """Convert one resource dict to a (logical_id, cfn_block) pair.

    Args:
      data: A duploctl resource dict. Should contain a 'kind' key
        so the CFN Type can be determined.

    Returns:
      A tuple of (logical_id, cfn_resource_dict).
    """
    kind = data.get("kind", "Resource")
    name = (
        data.get("Name")
        or data.get("AccountName")
        or data.get("FunctionName")
        or data.get("name")
        or "Resource"
    )
    logical_id = _logical_id(kind, name)
    tenant = data.get("TenantName") or data.get("tenant")

    body = {
        k: v
        for k, v in data.items()
        if k not in ("kind", "TenantName", "tenant")
    }

    props = {
        "ServiceToken": _DEFAULT_SERVICE_TOKEN,
        "Body": body,
        "Wait": True,
    }
    if tenant:
        props["Tenant"] = tenant

    return logical_id, {
        "Type": f"Custom::Duplo@{kind.capitalize()}",
        "Properties": props,
    }


def _logical_id(kind: str, name: str) -> str:
    """Generate a CamelCase CloudFormation logical resource ID.

    Args:
      kind: duploctl resource kind (e.g. 'service').
      name: Resource name (e.g. 'my-nginx').

    Returns:
      A valid CloudFormation logical ID string.
    """
    parts = [kind.capitalize()]
    for segment in name.replace("-", "_").split("_"):
        if segment:
            parts.append(segment.capitalize())
    return "".join(parts)
