ARG PY_VERSION=3.13

# Stage 1: Build the wheel
FROM python:${PY_VERSION} AS builder

WORKDIR /app

COPY . .

RUN <<EOF
apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir .[build]
python -m build --no-isolation
EOF

# Stage 2: Lambda runtime image
FROM public.ecr.aws/lambda/python:${PY_VERSION}

COPY --from=builder /app/dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# Lambda handler: module.function
CMD ["duplocloud.cfn.handler.handler"]
