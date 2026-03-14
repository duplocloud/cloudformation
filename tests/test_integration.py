"""Integration tests for the CloudFormation custom resource provider.

These tests require live DuploCloud credentials. They are organised into
an ordered class so each step is visible independently:

  Step 10 — ECR repo ensured (cfn apply_ecr)
  Step 20 — Docker image pushed to private ECR
  Step 30 — Lambda deployed (cfn apply_lambda)
  Step 40 — CFN stack created (CREATE_COMPLETE)
  Step 50 — CFN stack read back (cfn find)
  Step 60 — CFN stack listed (cfn list includes it)
  Step 70 — CFN stack deleted (DELETE_COMPLETE)
  Step 80 — Lambda cleaned up

Run full suite:
  pytest tests/test_integration.py -m integration -s

Cleanup leftover resources from a failed run:
  pytest tests/test_integration.py -m cleanup -s
"""

import base64
import json
import os
import random
import subprocess
import time

import pytest
from duplocloud.errors import DuploError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LAMBDA_NAME = "duploctl-cfn-integ"
ECR_REPO_NAME = "duploctl-cfn"
DOCKER_IMAGE = "duplocloud/duploctl-cfn:latest"
STACK_NAME_BASE = "duploctl-cfn-integration-test"
INFRA_NAME = "cfn-integ-infra"
TENANT_NAME = "cfn-integ"
MAX_STACK_SUFFIX = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_stack_name(cfn_plugin) -> str:
    """Return a usable stack name, skipping stuck DELETE_IN_PROGRESS stacks."""
    for i in range(MAX_STACK_SUFFIX + 1):
        name = (
            STACK_NAME_BASE if i == 0
            else f"{STACK_NAME_BASE}-{i}"
        )
        try:
            stack = cfn_plugin.find(name)
            if stack["StackStatus"] == "DELETE_IN_PROGRESS":
                print(
                    f"  Stack '{name}' stuck in DELETE_IN_PROGRESS,"
                    " trying next suffix"
                )
                continue
            return name
        except DuploError as exc:
            if exc.code == 404:
                return name
            raise
    return f"{STACK_NAME_BASE}-{MAX_STACK_SUFFIX}"


def _pick_cidr(infra_svc) -> str:
    """Return a /16 CIDR from 11.x.0.0/16 not already in use."""
    taken = {
        i.get("Vnet", {}).get("AddressPrefix", "")
        for i in infra_svc.list()
    }
    for _ in range(50):
        cidr = f"11.{random.randint(10, 250)}.0.0/16"
        if cidr not in taken:
            return cidr
    raise RuntimeError("Could not find a free /16 CIDR after 50 attempts")


def _stack_template(lambda_arn, infra_name, tenant_name, vpc_cidr) -> str:
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "duploctl-cfn integration test stack",
        "Resources": {
            "DuploInfra": {
                "Type": "Custom::Duplo@Infrastructure",
                "Properties": {
                    "ServiceToken": lambda_arn,
                    "Body": {
                        "Name": infra_name,
                        "Cloud": 0,
                        "Region": "us-west-2",
                        "EnableK8Cluster": False,
                        "EnableECSCluster": False,
                        "AzCount": 2,
                        "Vnet": {"AddressPrefix": vpc_cidr, "SubnetCidr": 22},
                    },
                    "Wait": True,
                },
            },
            "DuploTenant": {
                "Type": "Custom::Duplo@Tenant",
                "DependsOn": "DuploInfra",
                "Properties": {
                    "ServiceToken": lambda_arn,
                    # Name is required by name_from_body; AccountName by the API.
                    "Body": {"Name": tenant_name, "AccountName": tenant_name, "PlanID": infra_name},
                    "Wait": True,
                },
            },
        },
    }
    return json.dumps(template)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def stack_name(cfn_plugin) -> str:
    """Return a usable stack name (auto-increments past stuck stacks)."""
    return _active_stack_name(cfn_plugin)


# ---------------------------------------------------------------------------
# Integration test class — ordered CRUD steps
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.aws
class TestCfnIntegration:
    """End-to-end integration tests for duploctl-cfn.

    Steps run in numerical order; later steps depend on earlier ones.
    If a step fails, all dependent steps are automatically skipped.

      Step 10 — ECR repo applied (cfn apply_ecr)
      Step 20 — Docker image pushed to private ECR
      Step 30 — Lambda applied (cfn apply_lambda)
      Step 40 — CFN stack created (cfn create → CREATE_COMPLETE)
      Step 50 — CFN stack read back (cfn find)
      Step 60 — CFN stack listed (cfn list)
      Step 70 — CFN stack deleted (cfn delete → DELETE_COMPLETE)
      Step 80 — Lambda deleted
    """

    # -- Step 10: ECR repo -------------------------------------------------

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="cfn_ecr_repo")
    def test_apply_ecr(self, cfn_plugin):
        """Apply the ECR repo via cfn.apply_ecr() — creates if missing."""
        repo = cfn_plugin.apply_ecr(ECR_REPO_NAME)
        assert "RepositoryUri" in repo
        assert ECR_REPO_NAME in repo["RepositoryUri"]
        TestCfnIntegration.ecr_repo = repo

    # -- Step 20: Build and push Lambda image to private ECR ---------------

    @pytest.mark.order(20)
    @pytest.mark.dependency(name="cfn_ecr_push", depends=["cfn_ecr_repo"], scope="session")
    def test_push_image(self, ecr_boto):
        """Build the local Dockerfile and push to the private ECR repo.

        NOTE: This step differs from `duploctl cfn setup` on purpose.
        `setup` does NOT build anything — it pulls the public amd64 image
        from Docker Hub by content-addressed digest and re-publishes it
        to the portal’s ECR.  Here we build from the local source tree so
        the integration tests exercise the updated handler code before it
        has been released to Docker Hub.

        AWS Lambda (and the DuploCloud API) require Docker V2 schema 2
        single-architecture manifests.  `docker buildx build` with
        `--provenance=false` and `oci-mediatypes=false` produces exactly
        that without the OCI manifest-list that BuildKit emits by default.
        """
        repo_uri = self.ecr_repo["RepositoryUri"]
        registry = repo_uri.split("/")[0]
        image_uri = f"{repo_uri}:latest"

        token_resp = ecr_boto.get_authorization_token()
        auth = token_resp["authorizationData"][0]
        token = base64.b64decode(auth["authorizationToken"]).decode()
        username, password = token.split(":", 1)
        subprocess.run(
            ["docker", "login", "--username", username, "--password-stdin", registry],
            input=password.encode(),
            check=True,
            capture_output=True,
        )

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Build amd64 image directly into the registry using Docker V2
        # schema 2 media types (—not OCI) so Lambda accepts it.
        subprocess.run(
            [
                "docker", "buildx", "build",
                "--platform", "linux/amd64",
                "--provenance=false",
                f"--output=type=image,push=true,oci-mediatypes=false,name={image_uri}",
                ".",
            ],
            cwd=repo_root,
            check=True,
        )

        TestCfnIntegration.ecr_image_uri = image_uri

    # -- Step 30: Lambda ---------------------------------------------------

    @pytest.mark.order(30)
    @pytest.mark.dependency(name="cfn_lambda", depends=["cfn_ecr_push"], scope="session")
    def test_apply_lambda(self, cfn_plugin):
        """Apply the CFN lambda via cfn.apply_lambda() — creates or updates."""
        result = cfn_plugin.apply_lambda(
            lambda_name=LAMBDA_NAME,
            image=self.ecr_image_uri,
            wait=True,
        )
        assert "FunctionArn" in result
        assert LAMBDA_NAME in result["FunctionArn"]
        TestCfnIntegration.lambda_arn = result["FunctionArn"]

    # -- Step 40: Stack create ---------------------------------------------

    @pytest.mark.order(40)
    @pytest.mark.dependency(name="cfn_stack_create", depends=["cfn_lambda"], scope="session")
    def test_stack_create(self, cfn_plugin, stack_name, duplo):
        """Create the CFN stack and wait for CREATE_COMPLETE."""
        vpc_cidr = _pick_cidr(duplo.load("infrastructure"))
        template_body = _stack_template(
            self.lambda_arn, INFRA_NAME, TENANT_NAME, vpc_cidr
        )

        # Clean up any leftover stack (idempotent)
        try:
            cfn_plugin.delete(stack_name, wait=True)
        except Exception:
            pass

        try:
            leftover = cfn_plugin.find(stack_name)
            pytest.skip(
                f"Stack '{stack_name}' still in "
                f"{leftover['StackStatus']} — run cleanup first"
            )
        except DuploError as exc:
            if exc.code != 404:
                raise

        stack_body = {
            "StackName": stack_name,
            "TemplateBody": template_body,
            "Capabilities": ["CAPABILITY_IAM"],
            "OnFailure": "DO_NOTHING",
        }
        result = cfn_plugin.create(stack_body, wait=True)
        assert result["StackStatus"] == "CREATE_COMPLETE", (
            f"Stack ended in {result['StackStatus']}"
        )

    # -- Step 50: Stack find -----------------------------------------------

    @pytest.mark.order(50)
    @pytest.mark.dependency(name="cfn_stack_find", depends=["cfn_stack_create"], scope="session")
    def test_stack_find(self, cfn_plugin, stack_name):
        """Find the created stack by name and verify its fields."""
        stack = cfn_plugin.find(stack_name)
        assert stack["StackName"] == stack_name
        assert stack["StackStatus"] == "CREATE_COMPLETE"
        assert "StackId" in stack

    # -- Step 60: Stack list -----------------------------------------------

    @pytest.mark.order(60)
    @pytest.mark.dependency(name="cfn_stack_list", depends=["cfn_stack_create"], scope="session")
    def test_stack_list(self, cfn_plugin, stack_name):
        """List all stacks and confirm our stack appears."""
        stacks = cfn_plugin.list()
        names = [s.get("StackName") for s in stacks]
        assert stack_name in names

    # -- Step 70: Stack delete ---------------------------------------------

    @pytest.mark.order(70)
    @pytest.mark.dependency(name="cfn_stack_delete", depends=["cfn_stack_find"], scope="session")
    def test_stack_delete(self, cfn_plugin, stack_name):
        """Delete the CFN stack and verify DELETE_COMPLETE."""
        result = cfn_plugin.delete(stack_name, wait=True)
        assert "deleted" in result["message"].lower()

        try:
            cfn_plugin.find(stack_name)
            pytest.fail(f"Stack '{stack_name}' still exists after delete")
        except DuploError as exc:
            assert exc.code == 404

    # -- Step 80: Lambda delete --------------------------------------------

    @pytest.mark.order(80)
    @pytest.mark.dependency(name="cfn_lambda_delete", depends=["cfn_lambda"], scope="session")
    def test_lambda_delete(self, cfn_plugin):
        """Delete the integration test Lambda function."""
        lmb = cfn_plugin.duplo.load("lambda")
        full_name = lmb.name_from_body({"FunctionName": LAMBDA_NAME})
        lmb.delete(full_name)

        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                lmb.find(full_name)
                time.sleep(10)
            except DuploError:
                return  # gone — success
        pytest.fail(f"Lambda '{full_name}' still exists after 300s")



# ---------------------------------------------------------------------------
# Cleanup tests
#
# Run independently after a failed integration run to reset state:
#   pytest tests/test_integration.py -m cleanup -s
# ---------------------------------------------------------------------------

@pytest.mark.cleanup
def test_cleanup_duplo_resources(duplo):
    """Force-delete DuploCloud Tenant + Infrastructure the stack may have created."""
    infra_svc = duplo.load("infrastructure")
    tenant_svc = duplo.load("tenant")

    try:
        tenant_svc.find(TENANT_NAME)
        tenant_svc.delete(TENANT_NAME)
        print(f"  Deleted tenant: {TENANT_NAME}")
    except Exception:
        print(f"  Tenant '{TENANT_NAME}' not found — already clean")

    try:
        infra_svc.find(INFRA_NAME)
        infra_svc.delete(INFRA_NAME)
        print(f"  Deleted infra: {INFRA_NAME}")
    except Exception:
        print(f"  Infra '{INFRA_NAME}' not found — already clean")


@pytest.mark.cleanup
def test_cleanup_cfn_stacks(cfn_plugin):
    """Delete all CFN stack variants (base name + all suffixes).

    Scans base, base-1 through base-MAX_STACK_SUFFIX so every possible
    stuck stack is cleaned up in one pass.
    """
    names = [STACK_NAME_BASE] + [
        f"{STACK_NAME_BASE}-{i}" for i in range(1, MAX_STACK_SUFFIX + 1)
    ]
    for name in names:
        try:
            stack = cfn_plugin.find(name)
            print(
                f"  Stack '{name}' in {stack['StackStatus']}"
                " — deleting"
            )
            cfn_plugin.delete(name, force=True, wait=True)
            print(f"  Stack '{name}' deleted")
        except DuploError as exc:
            if exc.code == 404:
                print(f"  Stack '{name}' not found — already clean")
            else:
                print(f"  Stack '{name}' error: {exc}")


@pytest.mark.cleanup
def test_cleanup_lambda(cfn_plugin):
    """Delete the integration test Lambda function if it still exists."""
    lmb = cfn_plugin.duplo.load("lambda")
    full_name = lmb.name_from_body({"FunctionName": LAMBDA_NAME})
    try:
        lmb.find(full_name)
        lmb.delete(full_name)
        print(f"  Deleted Lambda: {full_name}")
    except Exception:
        print(f"  Lambda '{full_name}' not found — already clean")


@pytest.mark.cleanup
def test_cleanup_ecr_repo(cfn_plugin):
    """Delete the integration test ECR repository if it still exists."""
    ecr = cfn_plugin.duplo.load("ecr")
    try:
        ecr.find(ECR_REPO_NAME)
        ecr.delete(ECR_REPO_NAME)
        print(f"  Deleted ECR repo: {ECR_REPO_NAME}")
    except Exception:
        print(f"  ECR repo '{ECR_REPO_NAME}' not found — already clean")

