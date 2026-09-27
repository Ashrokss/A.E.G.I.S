"""
Amazon Bedrock embeddings, mirroring utils/model_client.py's ModelClient shape
so the knowledge-base pipeline degrades the same way the chat model does.

Kept separate from ModelClient because embeddings use invoke_model rather than
Converse, and because `.available` should say which capability is configured --
a deployment can have a chat model without an embeddings model.
"""
import hashlib
import json
import logging
import math
import os
import re

import boto3
from botocore.config import Config

logger = logging.getLogger("uvicorn.error")

_MOCK_DIMENSIONS = 256
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Without this list, "why did the ALB return 5xx errors during the scale-in
# event" and a completely unrelated RDS/Lambda postmortem still score
# moderately similar in mock mode, purely because both are English prose full
# of "the"/"was"/"during"/"did" -- high-frequency function words that say
# nothing about topic but dominate a raw bag-of-words vector. Filtering them
# out is what makes coverage/COVERAGE_FLOOR mean something in mock mode
# instead of being noise-dominated regardless of actual relevance.
_STOPWORDS = frozenset("""
a an the and or but if then else so because as until while of at by for with
about against between into through during before after above below to from
up down in out on off over under again further once here there when where
why how all any both each few more most other some such no nor not only own
same than too very s t can will just don don't should should've now d ll m
o re ve y ain aren couldn didn doesn hadn hasn haven isn ma mightn mustn
needn shan shouldn wasn weren won wouldn is am are was were be been being
have has had having do does did doing this that these those i me my myself
we our ours ourselves you your yours yourself yourselves he him his himself
she her hers herself it its itself they them their theirs themselves what
which who whom
""".split())


def _mock_embed(text: str) -> list[float]:
    """Deterministic, credential-free stand-in using the hashing trick: each
    non-stopword token increments a dimension chosen by hashing the token
    (log-dampened, so one repeated word can't drown out the rest), so two
    texts sharing actual CONTENT vocabulary land on genuinely similar
    vectors while two texts that only share function words do not. Hashing
    the whole string as one blob (the tempting simpler version) would give
    two different texts uncorrelated vectors regardless of content, which
    makes ranking in mock mode meaningless — this keeps mock mode a usable
    (if crude) keyword match, not just a plumbing smoke test."""
    vector = [0.0] * _MOCK_DIMENSIONS
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS or token.isdigit():
            continue
        idx = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % _MOCK_DIMENSIONS
        vector[idx] += 1.0
    # Term-frequency saturation (1 + log(count)) instead of a raw linear
    # count: a word appearing 10x in a long document shouldn't get 10x the
    # weight of one appearing once, or document length alone starts driving
    # similarity more than actual shared vocabulary does.
    vector = [math.log1p(v) if v else 0.0 for v in vector]
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


class EmbeddingsModelClient:
    """One configured embeddings deployment. `available` is False when
    credentials or a model name are absent, in which case embed_many() falls
    back to a deterministic mock vector rather than raising."""

    def __init__(self):
        self.region = (os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "").strip()
        self.model = os.getenv("BEDROCK_EMBED_MODEL", "").strip()
        # Titan v2 accepts 256/512/1024. The stored vectors' length is the
        # comparison key (_cosine refuses to compare across lengths), so
        # changing this means re-embedding the corpus.
        self.dimensions = int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024"))

        if self.model and self.region:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(retries={"max_attempts": 3, "mode": "standard"}),
            )
        else:
            self.client = None
            logger.warning(
                "BEDROCK_EMBED_MODEL/BEDROCK_REGION not set. "
                "Knowledge-base embeddings will run in mock mode."
            )

    @property
    def available(self) -> bool:
        return self.client is not None

    def embed_many(self, texts: list[str]) -> tuple[list[list[float]], str]:
        """Returns (vectors, model_label) — model_label is stamped on each
        chunk so a later re-embedding pass can tell mock vectors from real
        ones apart."""
        if not texts:
            return [], (self.model or "mock")
        if not self.client:
            return [_mock_embed(t) for t in texts], "mock"

        # Titan takes one inputText per call — there is no batch form. Callers
        # already run this inside asyncio.to_thread, so the loop doesn't block
        # the event loop.
        # ponytail: serial; a ThreadPoolExecutor if ingest latency ever bites.
        vectors = []
        for text in texts:
            response = self.client.invoke_model(
                modelId=self.model,
                body=json.dumps({
                    "inputText": text,
                    "dimensions": self.dimensions,
                    # Normalised vectors make _cosine exact rather than
                    # approximately right.
                    "normalize": True,
                }),
            )
            vectors.append(json.loads(response["body"].read())["embedding"])
        return vectors, self.model