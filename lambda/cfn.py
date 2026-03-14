"""CFN custom resource lifecycle handling (Create / Update / Delete)."""

import json
import logging
import os
import urllib.request

import jmespath

from duplocloud.controller import DuploCtl
from duplocloud.errors import DuploError

from .utils import extract_properties, get_id, parse_resource_type

logger = logging.getLogger(__name__)

CFN_SUCCESS = "SUCCESS"
CFN_FAILED = "FAILED"


def is_cfn_event(event: dict) -> bool:
    """Return True when *event* looks like a CloudFormation custom resource event.

    Duck-typing check: the presence of ``ResponseURL`` is the definitive
    marker because it is a pre-signed S3 URL that CFN always injects.

    Args:
        event: The raw Lambda event dict.

    Returns:
        ``True`` if this is a CFN lifecycle event.
    """
    return bool(event.get("ResponseURL"))


def send_response(
    response_url: str,
    status: str,
    request_id: str,
    stack_id: str,
    logical_id: str,
    physical_id: str,
    reason: str = "",
    data: dict = None,
) -> None:
    """Send a CFN custom resource response via HTTP PUT to *response_url*.

    Uses only ``urllib.request`` from the standard library so that no
    extra runtime dependency (e.g. ``requests``) is required.

    Args:
        response_url: Pre-signed S3 URL from the CFN event.
        status: ``"SUCCESS"`` or ``"FAILED"``.
        request_id: Echoed from ``event["RequestId"]``.
        stack_id: Echoed from ``event["StackId"]``.
        logical_id: Echoed from ``event["LogicalResourceId"]``.
        physical_id: Physical resource id (stable across Create/Update).
        reason: Human-readable failure reason (used when status=FAILED).
        data: Key/value dict exposed via ``Fn::GetAtt`` to the template.
    """
    body = {
        "Status": status,
        "RequestId": request_id,
        "StackId": stack_id,
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": physical_id,
    }
    if reason:
        body["Reason"] = reason
    if data:
        body["Data"] = data

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        response_url,
        data=payload,
        method="PUT",
    )
    # CFN requires Content-Type to be empty for the presigned URL
    req.add_header("Content-Type", "")
    req.add_header("Content-Length", str(len(payload)))
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        logger.debug("CFN response HTTP %s", resp.status)


def handle_cfn_event(event: dict, context) -> None:
    """Process a CloudFormation custom resource lifecycle event.

    Dispatches to Create/Update/Delete logic, guarantees a response is
    always sent to ``ResponseURL`` even on unhandled exceptions.

    Args:
        event: The CloudFormation custom resource event.
        context: The Lambda context object (provides
            ``get_remaining_time_in_millis()``).
    """
    response_url = event["ResponseURL"]
    request_id = event["RequestId"]
    stack_id = event["StackId"]
    logical_id = event["LogicalResourceId"]
    physical_id = event.get("PhysicalResourceId", "UNKNOWN")
    request_type = event.get("RequestType", "")

    data = None
    try:
        resource_type = event["ResourceType"]
        kind = parse_resource_type(resource_type)

        props = event.get("ResourceProperties", {})
        reserved, body = extract_properties(props)

        tenant = reserved.get("Tenant")
        do_wait = reserved.get("Wait", True)
        validate = reserved.get("Validate", True)
        query = reserved.get("Query")
        allow_import = reserved.get("AllowImport", True)

        duplo = DuploCtl(
            host=os.environ["DUPLO_HOST"],
            token=os.environ["DUPLO_TOKEN"],
            tenant=tenant,
        )
        duplo.validate = validate
        # Delete events never wait — fire-and-forget so the Lambda always
        # responds to CFN before timing out.  The DuploCloud deletion runs
        # asynchronously regardless.
        duplo.wait = do_wait and request_type != "Delete"

        # Respect Lambda remaining time for wait_timeout
        if context and hasattr(context, "get_remaining_time_in_millis"):
            remaining_ms = context.get_remaining_time_in_millis()
            # Leave a 15-second buffer for sending the response
            duplo.wait_timeout = max(1, (remaining_ms // 1000) - 15)
        logger.info(
            "DuploCtl configured: tenant=%s, wait=%s, wait_timeout=%s",
            tenant, duplo.wait,
            getattr(duplo, "wait_timeout", "default"),
        )

        resource = duplo.load(kind)

        if request_type in ("Create", "Update"):
            name = resource.name_from_body(body)
            logger.info(
                "%s %s '%s' (tenant=%s, wait=%s)",
                request_type, kind, name, tenant, do_wait,
            )
            if request_type == "Create" and not allow_import:
                try:
                    resource.find(name)
                    raise DuploError(
                        f"Resource '{name}' already exists and "
                        "AllowImport is false",
                        409,
                    )
                except DuploError as exc:
                    if exc.code == 409:
                        raise

            # Use explicit create/update instead of apply() to avoid
            # version-sensitive positional-arg differences in apply().
            existing = None
            try:
                existing = resource.find(name)
            except DuploError:
                pass
            if existing and request_type == "Update" and hasattr(resource, "update"):
                # Update event: apply desired state.
                resource.update(name, body)
            elif existing and request_type == "Create":
                # Create event with existing resource: AllowImport already
                # checked above — adopt it as-is, do not attempt an update.
                logger.info(
                    "Create %s '%s': resource exists, importing (AllowImport=true)",
                    kind, name,
                )
            elif not existing:
                resource.create(body)
            logger.info("%s %s '%s' applied", request_type, kind, name)

            found = resource.find(name)
            if query:
                data = jmespath.search(query, found)
            else:
                data = found
            # CFN response payload is capped at 4096 bytes total.
            # If the resource object is too large, send only the
            # physical resource id fields so Fn::GetAtt still works
            # for the most common use-case (referencing by name/id).
            if data and len(json.dumps(data)) > 3500:
                logger.warning(
                    "Response data too large (%d bytes), truncating "
                    "to PhysicalResourceId fields only",
                    len(json.dumps(data)),
                )
                data = {"Id": physical_id}
            physical_id = get_id(resource, found)
            logger.info(
                "%s %s '%s' done, PhysicalId=%s",
                request_type, kind, name, physical_id,
            )

        elif request_type == "Delete":
            if hasattr(resource, "delete"):
                name = resource.name_from_body(body)
                logger.info("Delete %s '%s' (tenant=%s)", kind, name, tenant)
                try:
                    resource.find(name)
                except DuploError:
                    logger.info(
                        "Delete %s '%s': already gone (idempotent)",
                        kind, name,
                    )
                else:
                    resource.delete(name)
                    logger.info(
                        "Delete %s '%s': delete call complete", kind, name
                    )
            else:
                logger.info(
                    "Delete %s: no delete() method, treating as no-op", kind
                )

        logger.info(
            "Sending CFN SUCCESS for %s %s (PhysicalId=%s)",
            request_type, logical_id, physical_id,
        )
        send_response(
            response_url,
            CFN_SUCCESS,
            request_id,
            stack_id,
            logical_id,
            physical_id,
            data=data if request_type != "Delete" else None,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "CFN handler error during %s of %s: %s",
            request_type, logical_id, exc,
        )
        send_response(
            response_url,
            CFN_FAILED,
            request_id,
            stack_id,
            logical_id,
            physical_id,
            reason=str(exc),
        )
