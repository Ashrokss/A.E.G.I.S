ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.11-slim
FROM ${PYTHON_BASE}

# Pinned: the agent's generated code is validated against this exact binary.
ARG TERRAFORM_VERSION=1.13.3
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends curl unzip git ca-certificates \
    && apt-get clean && find /var/lib/apt/lists -mindepth 1 -delete \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH:-amd64}.zip" -o /tmp/tf.zip \
    && unzip /tmp/tf.zip -d /usr/local/bin && unlink /tmp/tf.zip && terraform version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# uid 1000 matches the EFS access point owner (infra/terraform/modules/efs).
RUN useradd --uid 1000 --create-home aegis && mkdir -p data/runs && chown -R aegis:aegis /app
USER aegis

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
