import os
import sys

def validate_aws_agent_env():
    # Bedrock credentials are ambient (task role, or the local default chain),
    # so there is no key to check -- a model id and a region are the whole
    # configuration.
    auth_vars = ["AWS_COGNITO_JWKS_URL", "AWS_COGNITO_ISSUER_URL", "AWS_COGNITO_CLIENT_ID"]

    # Mirrors ModelClient's own resolution: BEDROCK_REGION falls back to
    # AWS_REGION, so checking only the former would report mock mode on a
    # deployment that is actually configured.
    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
    missing_llm = [name for name, present in (
        ("BEDROCK_MODEL_CHAT", os.environ.get("BEDROCK_MODEL_CHAT")),
        ("BEDROCK_REGION/AWS_REGION", region),
    ) if not present]
    missing_auth = [v for v in auth_vars if not os.environ.get(v)]

    print("\n--- STARTUP CONFIGURATION REPORT ---")
    print("Loaded Provider: AWS")
    print("Loaded Environment Sections: Global, AWS Agent, AWS Cognito Authentication")

    if missing_llm:
        print("⚠️  No LLM config (missing: " + ", ".join(missing_llm) + "). Agent will run in mock mode.", flush=True)
    else:
        print(f"✅ LLM configured (Bedrock {region}: "
              f"{os.environ.get('BEDROCK_MODEL_CHAT')})", flush=True)

    if missing_auth:
        print("⚠️  Cognito auth incomplete (missing: " + ", ".join(missing_auth) + "). "
              "Requests without a resolvable AWS account will be rejected.", flush=True)
    else:
        print("✅ Cognito auth configured.", flush=True)

    print("------------------------------------\n", flush=True)
