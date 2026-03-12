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
        duplo.wait = do_wait

        # Respect Lambda remaining time for wait_timeout
        if context and hasattr(context, "get_remaining_time_in_millis"):
            remaining_ms = context.get_remaining_time_in_millis()
            # Leave a 15-second buffer for sending the response
            duplo.wait_timeout = max(1, (remaining_ms // 1000) - 15)

        resource = duplo.load(kind)

        if request_type in ("Create", "Update"):
            if request_type == "Create" and not allow_import:
                name = resource.name_from_body(body)
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

            resource.apply(body)

            name = resource.name_from_body(body)
            found = resource.find(name)
            data = found
            if query:
                data = jmespath.search(query, found)
            physical_id = get_id(resource, found)

        elif request_type == "Delete":
            if hasattr(resource, "delete"):
                try:
                    name = resource.name_from_body(body)
                    resource.delete(name)
                except DuploError as exc:
                    if exc.code == 404:
                        logger.info(
                            "Resource already gone during Delete (idempotent)"
                        )
                    else:
                        raise
            else:
                logger.info(
                    "Resource kind '%s' has no delete(); treating as no-op",
                    kind,
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
        logger.exception("CFN handler error")
        send_response(
            response_url,
            CFN_FAILED,
            request_id,
            stack_id,
            logical_id,
            physical_id,
            reason=str(exc),
        )
