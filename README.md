# DuploCloud CloudFormation Provider

An AWS Lambda function that acts as a CloudFormation custom resource provider for [DuploCloud](https://duplocloud.com). Every resource available in [duploctl](https://github.com/duplocloud/duploctl) becomes a CloudFormation-manageable resource via `Custom::Duplo@<Kind>` resource types.

---

## How It Works

The Lambda function receives CloudFormation custom resource lifecycle events (Create, Update, Delete) and manages DuploCloud resources as part of a CloudFormation stack.

| CFN Event | duploctl action |
|-----------|----------------|
| Create    | `apply()` + `wait()` |
| Update    | `apply()` + `wait()` |
| Delete    | `delete()` (no-op if unsupported) |

`apply()` is idempotent — it finds the resource and updates it if it exists, or creates it if not. `wait()` ensures the underlying resource is fully provisioned before CloudFormation receives a SUCCESS response.

### ResourceType Naming Convention

`Custom::Duplo@<Kind>` — the `Kind` after `@` is lowercased and used directly as the duploctl resource name:

| CloudFormation ResourceType | duploctl resource |
|-----------------------------|-------------------|
| `Custom::Duplo@Service`     | `service`         |
| `Custom::Duplo@Tenant`      | `tenant`          |
| `Custom::Duplo@S3`          | `s3`              |
| `Custom::Duplo@Rds`         | `rds`             |
| `Custom::Duplo@Batch_Compute` | `batch_compute` |

### Two Invocation Modes

1. **CloudFormation mode** (default): Receives CFN lifecycle events identified by the `ResponseURL` field.
2. **Ad-hoc mode**: Receives pipe-style events via `aws lambda invoke`. Controlled by `DUPLO_ADHOC_ENABLED` (default: `true`).

---

## Deployment

### Container Image (recommended)

```bash
aws lambda create-function \
  --function-name duploctl \
  --package-type Image \
  --code ImageUri=duplocloud/cfn:latest \
  --role arn:aws:iam::123456789012:role/duploctl-lambda-role \
  --timeout 900 \
  --environment Variables="{DUPLO_HOST=https://myportal.duplocloud.net,DUPLO_TOKEN=...}"
```

### Lambda ZIP

Download the latest `duploctl-cfn-<version>.zip` from [GitHub Releases](https://github.com/duplocloud/cloudformation/releases).

```bash
aws lambda create-function \
  --function-name duploctl \
  --runtime python3.13 \
  --handler duplocloud.cfn.handler.handler \
  --role arn:aws:iam::123456789012:role/duploctl-lambda-role \
  --zip-file fileb://duploctl-cfn.zip \
  --timeout 900 \
  --environment Variables="{DUPLO_HOST=https://myportal.duplocloud.net,DUPLO_TOKEN=...}"
```

### Required IAM Permissions

The Lambda execution role needs:
- `lambda:InvokeFunction` (for ad-hoc callers, granted to the calling role)
- Logging: `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

No AWS API calls are made by the Lambda itself — all calls go to the DuploCloud portal API.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DUPLO_HOST` | Yes | DuploCloud portal URL (e.g. `https://myportal.duplocloud.net`) |
| `DUPLO_TOKEN` | Yes | DuploCloud API token |
| `DUPLO_ADHOC_ENABLED` | No | Enable ad-hoc invocation mode (default: `true`) |

---

## CFN Resource Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `Tenant` | String | — | Tenant name for tenant-scoped resources |
| `Wait` | Boolean | `true` | Wait for full provisioning before responding SUCCESS |
| `Validate` | Boolean | `true` | Enable client-side Pydantic model validation |
| `Query` | String | — | JMESPath expression to filter `Fn::GetAtt` output |
| `AllowImport` | Boolean | `true` | Allow adopting a pre-existing resource on Create |
| `Body` | Object | — | Explicit resource body; if omitted, remaining properties are used |

---

## CloudFormation Examples

### Deploy the Lambda function

```yaml
DuploctlLambda:
  Type: AWS::Lambda::Function
  Properties:
    FunctionName: duploctl
    PackageType: Image
    Code:
      ImageUri: duplocloud/cfn:latest
    Role: !GetAtt DuploctlRole.Arn
    Timeout: 900
    Environment:
      Variables:
        DUPLO_HOST: https://myportal.duplocloud.net
        DUPLO_TOKEN: !Sub "{{resolve:secretsmanager:duplo-token}}"
```

### Create a Tenant

```yaml
MyTenant:
  Type: Custom::Duplo@Tenant
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    AccountName: my-tenant
    PlanID: my-infra
```

### Create a Service (using Body key)

```yaml
MyService:
  Type: Custom::Duplo@Service
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Tenant: my-tenant
    Validate: false
    Body:
      Name: nginx
      Image: nginx:latest
      Replicas: 2
```

### Create Infrastructure

```yaml
MyInfra:
  Type: Custom::Duplo@Infrastructure
  Properties:
    ServiceToken: !GetAtt DuploctlLambda.Arn
    Name: my-infra
    Cloud: 0
    Region: us-east-1
    Vpc: "10.220.0.0/16"
    EnableK8Cluster: true

```

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
