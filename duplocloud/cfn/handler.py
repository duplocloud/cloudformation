"""Lambda entry point for the DuploCloud CloudFormation custom resource provider."""

import logging

from .adhoc import handle_adhoc_event
from .cfn import handle_cfn_event, is_cfn_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def handler(event: dict, context) -> object:
    """AWS Lambda handler for the DuploCloud CFN custom resource provider.

    Routes the incoming event to either the CloudFormation lifecycle
    handler or the ad-hoc (pipe-style) handler based on the event shape.

    CFN events are identified by the presence of a ``ResponseURL`` field
    (a pre-signed S3 URL always injected by CloudFormation).  All other
    events are treated as ad-hoc invocations and executed directly.

    Args:
        event: The raw Lambda event dict.
        context: The Lambda context object.

    Returns:
        For CFN events: ``None`` (the response is sent via HTTP PUT to
        ``ResponseURL``; returning a value has no effect).
        For ad-hoc events: The result of the duploctl command.
    """
    logger.info("Received event type: %s", type(event).__name__)
    if is_cfn_event(event):
        logger.info(
            "Routing to CFN handler (RequestType=%s)",
            event.get("RequestType"),
        )
        handle_cfn_event(event, context)
        return None

    logger.info("Routing to ad-hoc handler")
    return handle_adhoc_event(event)
