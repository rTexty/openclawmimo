"""LLM интеграция через OpenAI-совместимый API."""

import json
import sys

try:
    import requests
except ImportError:
    requests = None  # graceful degradation — _call_llm returns None

from brain._config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


def _extract_json(text):
    """
    Извлечь JSON из текста LLM-ответа.
    Ищет первый { ... } или [ ... ] с учётом вложенности и строковых литералов.
    Возвращает parsed object или None.
    """
    text = text.strip()
    # Ищем начало JSON
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        close_ch = "}" if ch == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        j = i
        while j < len(text):
            c = text[j]
            if escape:
                escape = False
                j += 1
                continue
            if c == "\\" and in_string:
                escape = True
                j += 1
                continue
            if c == '"' and not escape:
                in_string = not in_string
            elif not in_string:
                if c == ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i : j + 1])
                        except json.JSONDecodeError:
                            break
            j += 1
    return None


def _call_llm(system_prompt, user_prompt, temperature=0.0, max_tokens=2048):
    """Вызов LLM через OpenAI-совместимый API с retry/backoff."""
    import time

    if not LLM_BASE_URL or not LLM_API_KEY or requests is None:
        return None

    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            }
            payload = {
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 2**attempt  # 1s, 2s, 4s
                print(
                    f"LLM retry {attempt + 1}/{max_retries} after {delay}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(delay)
            else:
                print(
                    f"LLM error (all {max_retries} attempts failed): {e}",
                    file=sys.stderr,
                )
                return None
