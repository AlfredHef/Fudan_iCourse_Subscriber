#!/usr/bin/env python3
"""Check model routing before spending time on downloads, OCR and ASR."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check_models(providers: list[dict], client_factory) -> str:
    for provider in providers:
        client = client_factory(
            api_key=provider["api_key"], base_url=provider["base_url"],
            max_retries=0, timeout=30,
        )
        for model in provider["models"]:
            name = f"{provider['name']}/{model}"
            try:
                result = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=32,
                )
                if not result.choices:
                    raise ValueError("No completion choices returned")
            except Exception as error:
                print(f"Model check failed: {name} ({type(error).__name__}): {error}", flush=True)
                continue
            print(f"Model accepted a test request: {name}", flush=True)
            return name
    raise RuntimeError("No configured model can accept requests; stopping before lecture processing")


if __name__ == "__main__":
    from openai import OpenAI
    from src.runtime.config import resolve_model_providers

    check_models(resolve_model_providers(), OpenAI)
