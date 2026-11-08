"""Image-edit client spillover: base HTTP 429 -> one request to another endpoint."""

from __future__ import annotations

import argparse
import base64
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types import ImagesResponse


BASE_ENDPOINT = "https://<base-resource>.cognitiveservices.azure.com/openai/v1/"
SPILLOVER_ENDPOINT = "https://<spillover-resource>.cognitiveservices.azure.com/openai/v1/"
BASE_DEPLOYMENT = "gpt-image-base"
SPILLOVER_DEPLOYMENT = "gpt-image-spillover"
SCOPE = "https://cognitiveservices.azure.com/.default"
ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("client-spillover")


def validate_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https" or not parts.hostname or "<" in endpoint or ">" in endpoint
        or parts.username is not None or parts.password is not None
        or parts.path.rstrip("/") != "/openai/v1" or parts.query or parts.fragment
    ):
        raise ValueError("Configure an HTTPS endpoint ending in /openai/v1/, without credentials/query/fragment")
    return endpoint.rstrip("/") + "/"


def request_edit(
    client: OpenAI,
    deployment: str,
    image_path: Path,
    prompt: str,
    route: str,
    quality: Literal["low", "medium", "high"],
    size: str,
) -> ImagesResponse:
    LOG.info("attempt route=%s endpoint=%s deployment=%s", route, client.base_url, deployment)
    # Each attempt reopens the image because the first upload consumes the stream.
    with image_path.open("rb") as image:
        raw = client.images.with_raw_response.edit(
            model=deployment,
            image=image,
            prompt=prompt,
            n=1,
            quality=quality,
            size=size,
            output_format="jpeg",
        )
    LOG.info(
        "response route=%s HTTP=%s request_id=%s",
        route, raw.status_code, raw.headers.get("apim-request-id", raw.headers.get("x-request-id", "(absent)")),
    )
    return raw.parse()


def edit_with_spillover(
    base_client: OpenAI,
    spillover_client: OpenAI,
    image_path: Path,
    prompt: str,
    base_deployment: str = BASE_DEPLOYMENT,
    spillover_deployment: str = SPILLOVER_DEPLOYMENT,
    quality: Literal["low", "medium", "high"] = "low",
    size: str = "1152x864",
) -> tuple[ImagesResponse, str]:
    try:
        response = request_edit(base_client, base_deployment, image_path, prompt, "base", quality, size)
    except APIStatusError as error:
        LOG.warning(
            "response route=base HTTP=%s retry-after=%s retry-after-ms=%s body=%s",
            error.status_code, error.response.headers.get("retry-after", "(absent)"),
            error.response.headers.get("retry-after-ms", "(absent)"), error.response.text,
        )
        if error.status_code != 429:
            raise
        # Route by HTTP status, not error.code: the observed PTU code can be null.
        LOG.warning("client_spillover base_HTTP=429 -> route=spillover; no base retry")
    else:
        return response, "base"

    # Spillover errors propagate; there is no loop or fallback back to base.
    response = request_edit(
        spillover_client, spillover_deployment, image_path, prompt, "spillover", quality, size,
    )
    return response, "spillover"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-endpoint", default=os.getenv("BASE_ENDPOINT", BASE_ENDPOINT))
    parser.add_argument("--spillover-endpoint", default=os.getenv("SPILLOVER_ENDPOINT", SPILLOVER_ENDPOINT))
    parser.add_argument("--base-deployment", default=os.getenv("BASE_DEPLOYMENT", BASE_DEPLOYMENT))
    parser.add_argument("--spillover-deployment", default=os.getenv("SPILLOVER_DEPLOYMENT", SPILLOVER_DEPLOYMENT))
    parser.add_argument("--image", type=Path, required=True, help="Input image file")
    parser.add_argument("--prompt", type=Path, required=True, help="UTF-8 prompt text file")
    parser.add_argument("--quality", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--size", default="1152x864")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=ROOT / "output/client_spillover" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") / "edited.jpg")
    args = parser.parse_args()
    try:
        args.base_endpoint = validate_endpoint(args.base_endpoint)
        args.spillover_endpoint = validate_endpoint(args.spillover_endpoint)
    except ValueError as error:
        parser.error(str(error))
    if not args.base_deployment.strip() or not args.spillover_deployment.strip():
        parser.error("Both deployment names must be configured")
    if urlsplit(args.base_endpoint).hostname == urlsplit(args.spillover_endpoint).hostname:
        parser.error("This sample requires two different resource endpoints")
    if not args.image.is_file() or not args.prompt.is_file():
        parser.error("Input image and prompt files must exist")
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be positive and finite")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    prompt = args.prompt.read_text(encoding="utf-8")
    credential = AzureCliCredential()
    provider = get_bearer_token_provider(credential, SCOPE)
    try:
        with OpenAI(
            base_url=args.base_endpoint,
            api_key=os.getenv("BASE_AZURE_OPENAI_API_KEY", "").strip() or provider,
            max_retries=0,
            timeout=args.timeout,
        ) as base_client, OpenAI(
            base_url=args.spillover_endpoint,
            api_key=os.getenv("SPILLOVER_AZURE_OPENAI_API_KEY", "").strip() or provider,
            max_retries=0,
            timeout=args.timeout,
        ) as spillover_client:
            response, route = edit_with_spillover(
                base_client, spillover_client, args.image, prompt,
                args.base_deployment, args.spillover_deployment, args.quality, args.size,
            )
        if not response.data or len(response.data) != 1 or not response.data[0].b64_json:
            raise ValueError("Expected exactly one base64 image in the successful response")
        image_bytes = base64.b64decode(response.data[0].b64_json, validate=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(image_bytes)
        LOG.info("completed route=%s output=%s bytes=%s", route, args.output, len(image_bytes))
        return 0
    except (APIStatusError, APIConnectionError, ValueError, OSError) as error:
        LOG.error("Request failed without further retry: %s: %s", type(error).__name__, error)
        return 1
    finally:
        credential.close()


if __name__ == "__main__":
    raise SystemExit(main())
