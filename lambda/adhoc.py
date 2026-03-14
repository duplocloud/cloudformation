"""Ad-hoc Lambda invocation mode (pipe-style)."""

import logging
import os

from duplocloud.controller import DuploCtl

logger = logging.getLogger(__name__)

ADHOC_ENABLED_ENV = "DUPLO_ADHOC_ENABLED"


def is_adhoc_enabled() -> bool:
    """Return True unless ``DUPLO_ADHOC_ENABLED`` is explicitly disabled.

    Returns:
        ``False`` only when the env var is set to ``"false"``, ``"0"``,
        or ``"no"`` (case-insensitive).  All other values (including
        absent) are treated as enabled.
    """
    val = os.environ.get(ADHOC_ENABLED_ENV, "true").lower()
    return val not in ("false", "0", "no")


def handle_adhoc_event(event: dict) -> object:
    """Execute a pipe-style ad-hoc duploctl invocation.

    The event shape mirrors the Bitbucket pipe / MCP pattern:

    .. code-block:: json

        {
            "kind": "service",
            "cmd": "list",
            "name": "nginx",
            "tenant": "my-tenant",
            "args": "--wait --query '[].Name'",
            "body": {"Name": "nginx", "Image": "nginx:latest"}
        }

    Args:
        event: The raw Lambda event dict containing pipe-style fields.

    Returns:
        The raw result from the duploctl command (dict, list, or string).

    Raises:
        RuntimeError: If ad-hoc mode is disabled via
            ``DUPLO_ADHOC_ENABLED=false``.
    """
    if not is_adhoc_enabled():
        raise RuntimeError(
            "Ad-hoc invocation is disabled. "
            f"Set {ADHOC_ENABLED_ENV}=true to enable it."
        )

    kind = event.get("kind")
    if not kind:
        raise ValueError("Ad-hoc event must include 'kind'")

    cmd = event.get("cmd")
    name = event.get("name")
    tenant = event.get("tenant")
    extra_args = event.get("args", "")

    duplo = DuploCtl(
        host=os.environ["DUPLO_HOST"],
        token=os.environ["DUPLO_TOKEN"],
        tenant=tenant,
    )

    args = [kind]
    if cmd:
        args.append(cmd)
    if name:
        args.append(name)
    if extra_args:
        args.extend(extra_args.split())

    logger.info("Ad-hoc invocation: %s", args)
    return duplo(*args)
