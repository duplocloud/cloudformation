ARG PY_VERSION=3.13

# Stage 1: Lambda runtime image
FROM public.ecr.aws/lambda/python:${PY_VERSION} AS runner

# git is required to install the duplocloud-client git dependency in lambda/pyproject.toml
RUN dnf install -y git && dnf clean all

COPY lambda/ /app/lambda/

RUN pip install --no-cache-dir /app/lambda/ && rm -rf /app

# Lambda handler: module.function
CMD ["cfn_lambda.handler.handler"]
