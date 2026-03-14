"""Lambda entry point for the DuploCloud CloudFormation custom resource provider."""

import logging

from .adhoc import handle_adhoc_event
from .cfn import handle_cfn_event, is_cfn_event

# Lambda pre-initialises the root logger; basicConfig() is a no-op after
# that.  Set the level directly so our INFO messages are not suppressed.
logging.getLogger().setLevel(logging.INFO)
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
    if is_cfn_event(event):
        request_type = event.get("RequestType", "Unknown")
        resource_type = event.get("ResourceType", "Unknown")
        logical_id = event.get("LogicalResourceId", "Unknown")
        stack_id = event.get("StackId", "Unknown")
        logger.info(
            "CFN %s | %s | LogicalId=%s | Stack=%s",
            request_type,
            resource_type,
            logical_id,
            stack_id,
        )
        handle_cfn_event(event, context)
        logger.info(
            "CFN %s complete | %s | LogicalId=%s",
            request_type,
            resource_type,
            logical_id,
        )
        return None

    logger.info("Ad-hoc event: %s", event)
    return handle_adhoc_event(event)
