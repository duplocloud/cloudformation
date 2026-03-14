"""DuploCloud CloudFormation manager resource.

Manages CloudFormation stacks and the CFN lambda setup via the
duploctl-aws @Client for AWS authentication.
"""

import logging
import time

import boto3

from duplocloud.commander import Resource, Command
from duplocloud.controller import DuploCtl
from duplocloud.errors import DuploError
from duplocloud.resource import DuploResource
import duplocloud.args as args

logger = logging.getLogger(__name__)

LAMBDA_NAME = "duploctl-cfn"
ECR_REPO_NAME = "duploctl-cfn"
DEFAULT_PUBLIC_IMAGE = "duplocloud/duploctl-cfn:latest"

_TERMINAL = frozenset({
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "DELETE_COMPLETE",
    "CREATE_FAILED",
    "DELETE_FAILED",
    "UPDATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
})

_SUCCESS = frozenset({
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "DELETE_COMPLETE",
})


@Resource("cfn", client="aws")
class DuploCfn(DuploResource):
    """DuploCloud CloudFormation Manager.

    Manages CloudFormation stacks deployed via the DuploCloud CFN
    lambda. Also provides setup commands to install and configure
    the lambda itself.

    Uses the aws @Client for JIT-authenticated boto3 access.
    """

    def __init__(self, duplo: DuploCtl):
        super().__init__(duplo)
        self._cfn_client = None

    @property
    def cfn(self):
        """Lazy-loaded boto3 CloudFormation client."""
        if not self._cfn_client:
            self._cfn_client = self.client.load("cloudformation")
        return self._cfn_client

    def name_from_body(self, body: dict) -> str:
        """Extract the stack name from a body dict."""
        return (
            body.get("StackName")
            or body.get("Name")
            or ""
        )

    @Command()
    def setup(
        self,
        mode: str = "image",
        lambda_name: str = LAMBDA_NAME,
        image: str = None,
        wait: args.WAIT = True,
    ) -> dict:
        """Set up the DuploCloud CFN lambda.

        Orchestrates the full installation: ensures the ECR repo (image
        mode) or S3 bucket (zip mode) exists, then deploys the lambda.

        Usage: CLI Usage
          ```sh
          duploctl cfn setup
          duploctl cfn setup --mode image
          duploctl cfn setup --mode zip
          ```

        Args:
          mode: Deployment mode: 'image' (ECR container) or 'zip'.
          lambda_name: Lambda function name. Default: duploctl-cfn.
          image: Override ECR image URI (image mode only).
          wait: Wait for lambda to become active.

        Returns:
          message: Setup result including the function ARN.

        Raises:
          DuploError: If the mode is invalid or setup fails.
        """
        if mode == "image":
            repo = self.apply_ecr()
            image_uri = image or f"{repo['RepositoryUri']}:latest"
            return self.apply_lambda(
                lambda_name=lambda_name,
                image=image_uri,
                wait=wait,
            )
        if mode == "zip":
            bucket = self.apply_bucket()
            return self.apply_lambda(
                lambda_name=lambda_name,
                bucket=bucket["Name"],
                wait=wait,
            )
        raise DuploError(
            f"Unknown mode '{mode}'. Use 'image' or 'zip'.", 400
        )

    def apply_ecr(self, repo_name: str = ECR_REPO_NAME) -> dict:
        """Ensure the CFN ECR repository exists and return the repo object.

        Creates the repository if it is missing and polls until it is
        visible. Safe to call repeatedly (idempotent).

        Args:
          repo_name: ECR repository name. Default: duploctl-cfn.

        Returns:
          repo: The ECR repository object including RepositoryUri.

        Raises:
          DuploError: If the repo is not visible after 120 seconds.
        """
        ecr = self.duplo.load("ecr")
        try:
            repo = ecr.find(repo_name)
            logger.info("ECR repo '%s' already exists", repo_name)
            return repo
        except DuploError:
            ecr.create({"Name": repo_name})
            logger.info("Created ECR repo: %s", repo_name)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                return ecr.find(repo_name)
            except DuploError:
                time.sleep(5)
        raise DuploError(
            f"ECR repo '{repo_name}' not visible after 120s", 504
        )

    def apply_bucket(self, bucket_name: str = None) -> dict:
        """Ensure the CFN S3 bucket exists and return the bucket object.

        Derives the bucket name from the AWS account ID if not given.
        Safe to call repeatedly (idempotent).

        Args:
          bucket_name: Override the bucket name. Default: duploctl-cfn-{account_id}.

        Returns:
          bucket: The S3 bucket object including Name.

        Raises:
          DuploError: If bucket creation fails.
        """
        s3 = self.duplo.load("s3")
        if not bucket_name:
            sts = self.client.load("sts")
            account_id = sts.get_caller_identity()["Account"]
            bucket_name = f"duploctl-cfn-{account_id}"
        try:
            bucket = s3.find(bucket_name)
            logger.info("S3 bucket '%s' already exists", bucket_name)
            return bucket
        except DuploError:
            s3.create({"Name": bucket_name})
            logger.info("Created S3 bucket: %s", bucket_name)
            return s3.find(bucket_name)

    def apply_lambda(
        self,
        lambda_name: str = LAMBDA_NAME,
        image: str = None,
        bucket: str = None,
        wait: bool = True,
    ) -> dict:
        """Create or update the CFN lambda function.

        Supports both image (ECR container) and ZIP (S3) deployment modes
        depending on which of `image` or `bucket` is provided. Safe to
        call repeatedly (idempotent — updates if it already exists).

        Args:
          lambda_name: Lambda function name. Default: duploctl-cfn.
          image: ECR image URI for image-mode deployment.
          bucket: S3 bucket name for zip-mode deployment.
          wait: Wait for the Lambda to become Active.

        Returns:
          FunctionArn: ARN of the deployed lambda.
          message: Deployment result message.

        Raises:
          DuploError: If neither image nor bucket is supplied.
        """
        if not image and not bucket:
            raise DuploError(
                "Either 'image' or 'bucket' must be provided.", 400
            )
        lmb = self.duplo.load("lambda")
        env_vars = {
            "DUPLO_HOST": self.duplo.host,
            "DUPLO_TOKEN": self.duplo.token,
        }
        if image:
            body = {
                "FunctionName": lambda_name,
                "PackageType": "Image",
                "Description": (
                    "DuploCloud CloudFormation custom resource provider"
                ),
                "Timeout": 900,
                "MemorySize": 512,
                "Tags": {},
                "Environment": {"Variables": env_vars},
                "Code": {"ImageUri": image},
                "Layers": [],
                "ImageConfig": {},
            }
            mode_label = f"image mode ({image})"
        else:
            body = {
                "FunctionName": lambda_name,
                "Runtime": "python3.13",
                "Handler": "cfn_lambda.handler.handler",
                "Description": (
                    "DuploCloud CloudFormation custom resource provider"
                ),
                "Timeout": 900,
                "MemorySize": 512,
                "Tags": {},
                "Environment": {"Variables": env_vars},
                "Code": {
                    "S3Bucket": bucket,
                    "S3Key": "duploctl-cfn.zip",
                },
                "Layers": [],
            }
            mode_label = f"zip mode (s3://{bucket}/duploctl-cfn.zip)"

        full_name = lmb.name_from_body(body)
        try:
            lmb.find(full_name)
            if image:
                lmb.update_image(full_name, image)
            else:
                lmb.update_s3(full_name, bucket, "duploctl-cfn.zip")
            logger.info("Lambda '%s' updated (%s)", full_name, mode_label)
        except DuploError:
            lmb.create(body)
            logger.info("Lambda '%s' created (%s)", full_name, mode_label)

        # Use the AWS boto3 client to get FunctionArn and wait for active.
        # This avoids relying on Duplo's eventually-consistent lambda list.
        aws_lmb = self.client.load("lambda")
        if wait:
            waiter = aws_lmb.get_waiter("function_active_v2")
            waiter.wait(FunctionName=full_name)
            logger.info("Lambda '%s' is active", full_name)

        cfg = aws_lmb.get_function_configuration(FunctionName=full_name)
        return {
            "message": f"Lambda '{lambda_name}' deployed ({mode_label})",
            "FunctionArn": cfg["FunctionArn"],
        }

    @Command()
    def list(self) -> list:
        """List all CloudFormation stacks.

        Usage: CLI Usage
          ```sh
          duploctl cfn list
          ```

        Returns:
          list: Summary of all CloudFormation stacks.
        """
        paginator = self.cfn.get_paginator("list_stacks")
        stacks = []
        for page in paginator.paginate():
            stacks.extend(page.get("StackSummaries", []))
        return stacks

    @Command()
    def find(self, name: args.NAME) -> dict:
        """Find a CloudFormation stack by name.

        Usage: CLI Usage
          ```sh
          duploctl cfn find <name>
          ```

        Args:
          name: The CloudFormation stack name.

        Returns:
          stack: The CloudFormation stack object.

        Raises:
          DuploError: If the stack does not exist.
        """
        try:
            resp = self.cfn.describe_stacks(StackName=name)
            return resp["Stacks"][0]
        except Exception as exc:
            if "does not exist" in str(exc):
                raise DuploError(
                    f"Stack '{name}' not found", 404
                ) from exc
            raise

    @Command()
    def create(
        self,
        body: args.BODY,
        wait: args.WAIT = True,
    ) -> dict:
        """Deploy a new CloudFormation stack.

        Usage: CLI Usage
          ```sh
          duploctl cfn create -f stack.yaml
          ```

        Args:
          body: Stack definition with StackName and template.
          wait: Wait for CREATE_COMPLETE before returning.

        Returns:
          stack: The created stack object.

        Raises:
          DuploError: If stack creation fails.
        """
        name = self.name_from_body(body)
        logger.info("Creating stack '%s'", name)
        self.cfn.create_stack(**self._stack_kwargs(body))
        logger.info("Stack '%s' create initiated", name)
        if wait:
            self._wait_stack(name, "CREATE_COMPLETE")
        return self.find(name)

    @Command()
    def update(
        self,
        name: args.NAME,
        body: args.BODY,
        wait: args.WAIT = True,
    ) -> dict:
        """Update an existing CloudFormation stack.

        Usage: CLI Usage
          ```sh
          duploctl cfn update <name> -f stack.yaml
          ```

        Args:
          name: The stack name to update.
          body: Updated stack definition.
          wait: Wait for UPDATE_COMPLETE before returning.

        Returns:
          stack: The updated stack object.

        Raises:
          DuploError: If the update fails.
        """
        logger.info("Updating stack '%s'", name)
        try:
            self.cfn.update_stack(**self._stack_kwargs(body))
        except Exception as exc:
            if "No updates are to be performed" in str(exc):
                logger.info(
                    "Stack '%s': no changes needed", name
                )
                return self.find(name)
            raise
        if wait:
            self._wait_stack(name, "UPDATE_COMPLETE")
        return self.find(name)

    @Command()
    def delete(
        self,
        name: args.NAME,
        wait: args.WAIT = True,
        force: bool = False,
    ) -> dict:
        """Delete a CloudFormation stack.

        Usage: CLI Usage
          ```sh
          duploctl cfn delete <name>
          duploctl cfn delete <name> --force
          ```

        Args:
          name: The stack name to delete.
          wait: Wait for DELETE_COMPLETE before returning.
          force: Use FORCE_DELETE_STACK deletion mode.

        Returns:
          message: Deletion result.

        Raises:
          DuploError: If delete fails.
        """
        logger.info("Deleting stack '%s' (force=%s)", name, force)
        kwargs = {"StackName": name}
        try:
            stack = self.find(name)
        except DuploError:
            return {"message": f"Stack '{name}' already gone (idempotent)"}
        # FORCE_DELETE_STACK is only valid when the stack is DELETE_FAILED
        if force and stack.get("StackStatus") == "DELETE_FAILED":
            kwargs["DeletionMode"] = "FORCE_DELETE_STACK"
        self.cfn.delete_stack(**kwargs)
        if wait:
            self._wait_stack(name, "DELETE_COMPLETE")
        return {"message": f"Stack '{name}' deleted"}

    @Command()
    def apply(
        self,
        body: args.BODY,
        wait: args.WAIT = True,
    ) -> dict:
        """Create or update a CloudFormation stack (idempotent).

        Usage: CLI Usage
          ```sh
          duploctl cfn apply -f stack.yaml
          ```

        Args:
          body: Stack definition.
          wait: Wait for completion before returning.

        Returns:
          stack: The resulting stack object.
        """
        name = self.name_from_body(body)
        try:
            self.find(name)
            return self.update(name, body, wait)
        except DuploError as exc:
            if exc.code == 404:
                return self.create(body, wait)
            raise

    @Command()
    def logs(
        self,
        lambda_name: str = LAMBDA_NAME,
        stream: args.STREAM = False,
        lines: int = 50,
    ) -> list:
        """Show CloudWatch logs for the CFN lambda.

        Retrieves recent log events from the lambda's CloudWatch log
        group and displays them in a clean terminal-friendly format.
        Optionally streams new events continuously.

        Usage: CLI Usage
          ```sh
          duploctl cfn logs
          duploctl cfn logs --stream
          duploctl cfn logs --lambda-name duploctl-cfn
          ```

        Args:
          lambda_name: Lambda function name. Default: duploctl-cfn.
          stream: Continuously stream new log events.
          lines: Number of recent log events to show.

        Returns:
          list: Log events with timestamp, stream, and message.

        Raises:
          DuploError: If the log group is not found.
        """
        lmb = self.duplo.load("lambda")
        full_name = lmb.name_from_body(
            {"FunctionName": lambda_name}
        )
        log_group = f"/aws/lambda/{full_name}"
        cw = self.client.load("logs")
        logger.info("Fetching logs from %s", log_group)
        events = self._fetch_log_events(cw, log_group, lines)
        if stream:
            return self._stream_logs(cw, log_group, events)
        return events

    def _fetch_log_events(
        self, cw, log_group: str, limit: int
    ) -> list:
        """Fetch the most recent log events from a CloudWatch group."""
        try:
            streams_resp = cw.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__:
                raise DuploError(
                    f"Log group '{log_group}' not found", 404
                ) from exc
            raise

        streams = streams_resp.get("logStreams", [])
        if not streams:
            return []

        events = []
        for stream_info in streams:
            resp = cw.get_log_events(
                logGroupName=log_group,
                logStreamName=stream_info["logStreamName"],
                limit=limit,
                startFromHead=False,
            )
            for evt in resp.get("events", []):
                events.append({
                    "timestamp": evt["timestamp"],
                    "stream": stream_info["logStreamName"],
                    "message": evt["message"].rstrip(),
                })
            if len(events) >= limit:
                break
        events.sort(key=lambda e: e["timestamp"])
        return events[-limit:]

    def _stream_logs(
        self, cw, log_group: str, initial: list
    ) -> list:
        """Stream log events continuously until interrupted."""
        seen = {e["message"] for e in initial}
        for evt in initial:
            print(
                f"[{evt['stream']}] {evt['message']}"
            )
        start_time = int(time.time() * 1000)
        try:
            while True:
                time.sleep(5)
                events = self._fetch_log_events(cw, log_group, 20)
                for evt in events:
                    if (
                        evt["timestamp"] >= start_time
                        and evt["message"] not in seen
                    ):
                        seen.add(evt["message"])
                        print(
                            f"[{evt['stream']}] {evt['message']}"
                        )
                start_time = int(time.time() * 1000) - 5000
        except KeyboardInterrupt:
            pass
        return initial

    def _wait_stack(
        self, name: str, target: str, timeout: int = 900
    ) -> str:
        """Poll until stack reaches target status or raises on failure.

        Args:
          name: Stack name.
          target: Desired terminal status.
          timeout: Maximum wait time in seconds.

        Returns:
          The final stack status string.

        Raises:
          DuploError: On stack failure or timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.cfn.describe_stacks(StackName=name)
            except Exception as exc:
                if (
                    "does not exist" in str(exc)
                    and target == "DELETE_COMPLETE"
                ):
                    return target
                raise
            status = resp["Stacks"][0]["StackStatus"]
            logger.info("Stack '%s' status: %s", name, status)
            if status == target:
                return status
            if (
                status == "DELETE_COMPLETE"
                and target != "DELETE_COMPLETE"
            ):
                raise DuploError(
                    f"Stack '{name}' was deleted unexpectedly",
                    500,
                )
            failed = (
                status.endswith("_FAILED")
                or status == "ROLLBACK_COMPLETE"
                or status == "UPDATE_ROLLBACK_COMPLETE"
            )
            if failed:
                events = self.cfn.describe_stack_events(
                    StackName=name
                )
                reasons = [
                    (
                        f"[{e.get('LogicalResourceId', '')}] "
                        f"{e.get('ResourceStatus', '')}: "
                        f"{e.get('ResourceStatusReason', '')}"
                    )
                    for e in events["StackEvents"]
                    if "FAILED" in e.get("ResourceStatus", "")
                ]
                raise DuploError(
                    f"Stack '{name}' reached {status}.\n"
                    + "\n".join(reasons),
                    500,
                )
            time.sleep(15)
        raise DuploError(
            f"Stack '{name}' did not reach {target} "
            f"within {timeout}s",
            504,
        )

    @staticmethod
    def _stack_kwargs(body: dict) -> dict:
        """Build create_stack/update_stack kwargs from body."""
        return {k: v for k, v in body.items() if v is not None}
