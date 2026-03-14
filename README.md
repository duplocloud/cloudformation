# DuploCloud CloudFormation

Two independent components live in this repository:

| Component | What it is |
|-----------|-----------|
| **`duploctl-cfn`** (this package) | A `duploctl` plugin that manages CloudFormation stacks and installs the CFN lambda |
| **`cfn-lambda`** (`lambda/`) | The AWS Lambda that acts as a CloudFormation custom resource provider |

The `Dockerfile` / `docker-compose.yaml` at the repo root build **only the Lambda image**.

---

## Quick start — manager plugin

```sh
pip install duploctl-cfn

export DUPLO_HOST=https://myportal.duplocloud.net
export DUPLO_TOKEN=...
export DUPLO_TENANT=default

# Deploy the CFN lambda (image mode — pulls from ECR)
duploctl cfn setup

# Deploy the CFN lambda (zip mode — pulls from S3)
duploctl cfn setup --mode zip

# Create / update a stack
duploctl cfn apply -f stack.yaml

# List stacks
duploctl cfn list

# Find a single stack
duploctl cfn find my-stack
```

---

## Lambda setup — image mode vs zip mode

The `setup` command deploys the Lambda function that handles CloudFormation custom resource events.

### Image mode (default)

```sh
duploctl cfn setup
```

What it does:
1. Ensures the `duploctl-cfn` ECR repository exists in the current tenant
2. Pulls the `linux/amd64` digest of `duplocloud/duploctl-cfn:latest` from Docker Hub and pushes it to the private ECR
3. Deploys (or updates) the Lambda function using the ECR image

> **Why single-arch?** AWS Lambda only accepts Docker V2 single-architecture
> manifests. The public image is multi-arch (amd64 + arm64); the setup command
> extracts and pushes only the `amd64` image by its content-addressed digest to
> avoid the OCI manifest-list rejection.

Individual steps can also be called directly:

```sh
# Ensure ECR repo
duploctl cfn apply_ecr

# Deploy lambda (after ECR image is pushed)
duploctl cfn apply_lambda --lambda-name duploctl-cfn \
  --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/duploctl-cfn:latest
```

### Zip mode

```sh
duploctl cfn setup --mode zip
```

What it does:
1. Ensures an S3 bucket exists (derived from the AWS account ID)
2. Uploads `duploctl-cfn.zip` to the bucket
3. Deploys the Lambda using the ZIP artifact

```sh
# Target a specific bucket
duploctl cfn setup --mode zip --bucket my-artifacts-bucket
```

---

## Building and publishing the Lambda image

The Docker image is built with `docker buildx bake`. The `--provenance=false` flag is
**required** so the image is pushed as a plain Docker V2 Schema 2 manifest (not an OCI
manifest list), which AWS Lambda requires:

```sh
# Build and push to Docker Hub
docker buildx bake --provenance=false --push
```

### Pushing to a private ECR

Lambda requires images in the **same AWS account and region**. Use `duploctl cfn setup`
to automatically pull the `linux/amd64` image from Docker Hub and push it to the portal's ECR:

```sh
duploctl cfn setup
```

Or manually:

```sh
REPO=123456789012.dkr.ecr.us-east-1.amazonaws.com/duploctl-cfn

aws ecr get-login-password | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com
docker pull --platform linux/amd64 duplocloud/duploctl-cfn:latest
docker tag duplocloud/duploctl-cfn:latest $REPO:latest
docker push $REPO:latest
```

---

## CFN stack format

CFN stacks are expressed as standard CloudFormation YAML/JSON. `apply` accepts a body with
`StackName` + `TemplateBody` (or `TemplateURL`):

```sh
duploctl cfn apply -f my-stack.yaml
```

```yaml
# my-stack.yaml
StackName: my-app-stack
TemplateBody: |
  AWSTemplateFormatVersion: "2010-09-09"
  Resources:
    MyTenant:
      Type: Custom::Duplo@Tenant
      ...
Capabilities:
  - CAPABILITY_IAM
```

---

## CloudFormation custom resource reference

### Resource type naming

`Custom::Duplo@<Kind>` — the `Kind` after `@` maps to the `duploctl` resource name (case-insensitive):

| CloudFormation Type | duploctl resource |
|---------------------|-------------------|
| `Custom::Duplo@Tenant` | `tenant` |
| `Custom::Duplo@Infrastructure` | `infrastructure` |
| `Custom::Duplo@Service` | `service` |
| `Custom::Duplo@S3` | `s3` |
| `Custom::Duplo@Rds` | `rds` |
| `Custom::Duplo@Batch_Compute` | `batch_compute` |

### Resource properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `ServiceToken` | String | — | Lambda ARN |
| `Tenant` | String | — | Tenant name (for tenant-scoped resources) |
| `Body` | Object | — | Resource body; if omitted, all other properties are used |
| `Wait` | Boolean | `true` | Wait for provisioning to complete before returning |
| `Query` | String | — | JMESPath expression applied to `Fn::GetAtt` output |
| `AllowImport` | Boolean | `true` | Adopt a pre-existing resource on Create |

### Examples

#### Create a Tenant inside an Infrastructure

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Resources:

  DuploInfra:
    Type: Custom::Duplo@Infrastructure
    Properties:
      ServiceToken: !GetAtt DuploctlLambda.Arn
      Wait: true
      Body:
        Name: my-infra
        Cloud: 0
        Region: us-east-1
        EnableK8Cluster: false
        Vnet:
          AddressPrefix: "10.100.0.0/16"
          SubnetCidr: 22

  DuploTenant:
    Type: Custom::Duplo@Tenant
    DependsOn: DuploInfra
    Properties:
      ServiceToken: !GetAtt DuploctlLambda.Arn
      Wait: true
      Body:
        Name: my-tenant
        AccountName: my-tenant
        PlanID: my-infra
```

#### Deploy a Service into a Tenant

```yaml
MyService:
  Type: Custom::Duplo@Service
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Body:
      Name: nginx
      Image: nginx:latest
      Replicas: 2
```

#### Query a stack output with JMESPath

```yaml
MyBucket:
  Type: Custom::Duplo@S3
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Query: BucketName
    Body:
      Name: my-bucket
```

---

## Lambda environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DUPLO_HOST` | Yes | Portal URL (`https://myportal.duplocloud.net`) |
| `DUPLO_TOKEN` | Yes | DuploCloud API token |
| `DUPLO_ADHOC_ENABLED` | No | Enable direct `aws lambda invoke` calls (default: `true`) |

### Use Fn::GetAtt with Query

```yaml
MySecret:
  Type: Custom::Duplo@Secret
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Query: "SecretArn"
    Body:
      Name: my-secret
      Data: !Sub "{{resolve:secretsmanager:my-plaintext-secret}}"

MyApp:
  Type: Custom::Duplo@Service
  DependsOn: MySecret
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Body:
      Name: my-app
      Image: my-app:latest
      EnvVariables:
        - Name: SECRET_ARN
          Value: !GetAtt MySecret.SecretArn
```

### Disable validation for resources without models

```yaml
MyRds:
  Type: Custom::Duplo@Rds
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Validate: false
    Body:
      Name: mydb
      Engine: mysql
      EngineVersion: "8.0"
      MasterUsername: admin
```

### Fire-and-forget (Wait: false)

```yaml
MyS3Bucket:
  Type: Custom::Duplo@S3
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Wait: false
    Body:
      Name: my-bucket
```

---

## Ad-Hoc Usage

When `DUPLO_ADHOC_ENABLED=true`, you can invoke the Lambda directly with a pipe-style event:

```bash
# List services
aws lambda invoke \
  --function-name duploctl \
  --payload '{"kind": "service", "cmd": "list", "tenant": "my-tenant"}' \
  response.json

# Find a specific service
aws lambda invoke \
  --function-name duploctl \
  --payload '{"kind": "service", "cmd": "find", "name": "nginx", "tenant": "my-tenant"}' \
  response.json

# Apply a resource
aws lambda invoke \
  --function-name duploctl \
  --payload '{
    "kind": "service",
    "cmd": "apply",
    "tenant": "my-tenant",
    "body": {"Name": "nginx", "Image": "nginx:latest"}
  }' \
  response.json

# List tenants (portal-scoped)
aws lambda invoke \
  --function-name duploctl \
  --payload '{"kind": "tenant", "cmd": "list"}' \
  response.json
```

---

## Available Resources

All resources registered in duploctl are available. See [cli.duplocloud.com](https://cli.duplocloud.com) for the full reference.

The naming convention is: `Custom::Duplo@<Resource>` where `<Resource>` corresponds directly to the duploctl resource name (case-insensitive).

---

## Development

```bash
# Install with test dependencies
pip install --editable '.[build,test]'

# Run tests
pytest tests

# Lint
ruff check ./duplocloud
```

### Project Structure

```
duplocloud/cfn/
├── handler.py   # Lambda entry point — routes CFN vs ad-hoc events
├── cfn.py       # CFN lifecycle handling (Create/Update/Delete + response)
├── adhoc.py     # Ad-hoc pipe-style invocation
└── utils.py     # ResourceType parsing, property extraction, get_id()
```
