"""Integration tests for the CloudFormation custom resource provider.

These tests require live DuploCloud credentials. They are organised into
an ordered class so each step is visible independently:

  Step 10 — Public DockerHub image copied to portal ECR (cfn copy_image)
  Step 20 — Lambda deployed (cfn apply_lambda)
  Step 30 — CFN stack created (CREATE_COMPLETE)
  Step 40 — CFN stack read back (cfn find)
  Step 50 — CFN stack listed (cfn list includes it)
  Step 60 — CFN stack deleted (DELETE_COMPLETE)
  Step 70 — Lambda cleaned up

Run full suite:
  pytest tests/test_integration.py -m integration -s

Cleanup leftover resources from a failed run:
  pytest tests/test_integration.py -m cleanup -s
"""

import json
import random
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
                    "Body": {"AccountName": tenant_name, "PlanID": infra_name},
                    "Wait": True,
                    "Force": True,
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

      Step 10 — Public DockerHub image copied to portal ECR (cfn copy_image)
      Step 20 — Lambda deployed (cfn apply_lambda)
      Step 30 — CFN stack created (CREATE_COMPLETE)
      Step 40 — CFN stack read back (cfn find)
      Step 50 — CFN stack listed (cfn list includes it)
      Step 60 — CFN stack deleted (DELETE_COMPLETE)
      Step 70 — Lambda cleaned up

    NOTE: No Docker images are built here. The public image
    ``duplocloud/duploctl-cfn:latest`` must be published to Docker Hub
    first via ``docker buildx bake --push``. ``copy_image`` pulls the
    ``linux/amd64`` digest from Docker Hub and re-publishes it to the
    portal's ECR (Lambda requires images in the same account + region).
    """

    ecr_image_uri: str = None
    lambda_arn: str = None

    # -- Step 10: Copy public image to ECR ---------------------------------

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="cfn_ecr_push")
    def test_copy_image(self, cfn_plugin):
        """Pull linux/amd64 from Docker Hub and push to the portal's ECR."""
        result = cfn_plugin.copy_image()
        assert "image_uri" in result
        assert ECR_REPO_NAME in result["image_uri"]
        TestCfnIntegration.ecr_image_uri = result["image_uri"]

    # -- Step 20: Lambda ---------------------------------------------------

    @pytest.mark.order(20)
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

    # -- Step 30: Stack create ---------------------------------------------

    @pytest.mark.order(30)
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

    # -- Step 40: Stack find -----------------------------------------------

    @pytest.mark.order(40)
    @pytest.mark.dependency(name="cfn_stack_find", depends=["cfn_stack_create"], scope="session")
    def test_stack_find(self, cfn_plugin, stack_name):
        """Find the created stack by name and verify its fields."""
        stack = cfn_plugin.find(stack_name)
        assert stack["StackName"] == stack_name
        assert stack["StackStatus"] == "CREATE_COMPLETE"
        assert "StackId" in stack

    # -- Step 50: Stack list -----------------------------------------------

    @pytest.mark.order(50)
    @pytest.mark.dependency(name="cfn_stack_list", depends=["cfn_stack_create"], scope="session")
    def test_stack_list(self, cfn_plugin, stack_name):
        """List all stacks and confirm our stack appears."""
        stacks = cfn_plugin.list()
        names = [s.get("StackName") for s in stacks]
        assert stack_name in names

    # -- Step 60: Stack delete ---------------------------------------------

    @pytest.mark.order(60)
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

    # -- Step 70: Lambda delete --------------------------------------------

    @pytest.mark.order(70)
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
        tenant_svc.delete(TENANT_NAME, force=True)
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

