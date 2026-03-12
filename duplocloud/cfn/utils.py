"""Utility functions for the DuploCloud CFN provider."""

RESERVED_PROPERTIES = frozenset([
    "Tenant",
    "Wait",
    "Validate",
    "Query",
    "AllowImport",
    "Body",
])


def parse_resource_type(resource_type: str) -> str:
    """Parse a CFN ResourceType string and return the duploctl resource kind.

    The convention is ``Custom::Duplo@<Kind>`` where ``<Kind>`` is
    case-insensitive and maps 1-to-1 to duploctl resource names.

    Args:
        resource_type: The CloudFormation ``ResourceType`` string,
            e.g. ``Custom::Duplo@Service``.

    Returns:
        The lowercased resource kind, e.g. ``service``.

    Raises:
        ValueError: If the resource type does not contain ``@``.
    """
    if "@" not in resource_type:
        raise ValueError(
            f"Invalid ResourceType '{resource_type}'. "
            "Expected format: Custom::Duplo@<Kind>"
        )
    _, kind = resource_type.split("@", 1)
    return kind.lower()


def extract_properties(properties: dict) -> tuple[dict, dict]:
    """Split CFN ResourceProperties into reserved keys and the resource body.

    Reserved keys (``Tenant``, ``Wait``, ``Validate``, ``Query``,
    ``AllowImport``, ``Body``) are extracted first.  If ``Body`` is
    present it is used as-is for the resource body; otherwise the
    remaining non-reserved properties form the body.

    CFN coerces all values to strings, so ``"true"``/``"false"`` are
    converted back to ``bool`` for the known boolean reserved keys.

    Args:
        properties: The raw ``ResourceProperties`` dict from the CFN event.

    Returns:
        A two-tuple ``(reserved, body)`` where *reserved* contains the
        extracted control properties and *body* is the resource payload.
    """
    props = dict(properties or {})
    props.pop("ServiceToken", None)
    props.pop("ServiceTimeout", None)

    reserved = {}
    for key in RESERVED_PROPERTIES:
        if key in props:
            reserved[key] = props.pop(key)

    # Coerce boolean strings for known boolean fields
    for bool_key in ("Wait", "Validate", "AllowImport"):
        if bool_key in reserved:
            val = reserved[bool_key]
            if isinstance(val, str):
                reserved[bool_key] = val.lower() not in ("false", "0", "no")

    body = reserved.pop("Body", None) or props
    return reserved, body


def get_id(resource, data: dict) -> str:
    """Return a deterministic CFN physical resource ID for a resource.

    The format is scope-aware:

    * Tenant-scoped:  ``<slug>::<tenant>::<name>``
    * Portal-scoped:  ``<slug>::<name>``

    This function is intentionally kept in the CFN project rather than in
    the duplocloud-client core so that core changes are not required.

    Args:
        resource: A loaded duploctl resource object (e.g. the result of
            ``duplo.load("service")``).
        data: The resource data dict returned from ``find()``.

    Returns:
        A stable, human-readable string identifier.
    """
    slug = getattr(resource, "slug", None) or type(resource).__name__.lower()

    # name_from_body handles both V2 (body["Name"]) and V3
    # (body["metadata"]["name"]) conventions.
    try:
        name = resource.name_from_body(data)
    except (KeyError, AttributeError):
        name = str(data)

    tenant = getattr(resource, "_tenant", None)
    if tenant and isinstance(tenant, dict):
        tenant = tenant.get("AccountName")
    if tenant:
        return f"{slug}::{tenant}::{name}"
    return f"{slug}::{name}"
