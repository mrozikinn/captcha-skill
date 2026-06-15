from __future__ import annotations

"""global-captcha /solve backend - hybrid OCR + LLM image captcha solver.

Hybrid OCR + LLM strategy
--------------------------

For `image_text` and `math` captcha types, Tesseract OCR runs first
(free, ~200ms). If Tesseract's per-word confidence is high and the
result looks like a real captcha token, it's returned immediately -
no LLM call, no tokens spent. Otherwise the solver falls through to
the LLM backend. For multi-cell reasoning captchas (hCaptcha grid,
reCAPTCHA v2 image, object_select), OCR is skipped and the LLM is used
directly.

Default backend (since v1.3) is `hermes chat -q --image`, which inherits
the user's hermes config (model, provider, YOLO mode). Pass use_hermes_cli=
False to solve_with_fallback() to force the HTTP router chain instead.

Config files (loaded in order, later overrides earlier):
  1. `config.yaml`      - template, ships in the repo, has placeholders
  2. `config.local.yaml`- your real values, gitignored
  3. Environment vars   - `CAPTCHA_ROUTER_URL`, `CAPTCHA_ROUTER_KEY`, etc.

The solver refuses to start if placeholders are still in the effective
config - see `_validate_config()` for details.

Run modes
---------

CLI test (one captcha):
    python3 solver.py --test captcha.png --type image_text

FastAPI server (default 127.0.0.1:8002):
    python3 solver.py
    curl -s -X POST http://127.0.0.1:8002/solve \\
         -H 'Content-Type: application/json' \\
         -d '{"image":"data:image/png;base64,...","type":"image_text"}'
"""

import io
import argparse
import json
import logging
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config(path: str | Path = None) -> dict:
    """Load YAML config + apply env var overrides.

    Config resolution order (later wins):
      1. config.yaml (template, ships in repo)
      2. config.local.yaml (gitignored, real values) — used if it exists
      3. Environment variables (CAPTCHA_*)

    Env var overrides (every one is optional):
      CAPTCHA_ROUTER_URL   overrides router_url
      CAPTCHA_ROUTER_KEY   overrides router_key
      CAPTCHA_PRIMARY      overrides models.primary
      CAPTCHA_FALLBACK     comma-separated list, overrides models.fallback
      CAPTCHA_TIMEOUT_S    overrides timeout_s
    """
    import yaml

    # 1. Resolve which config file to use
    if path:
        p = Path(path)
    else:
        # SOLVER_CONFIG env var can force a specific file
        forced = os.environ.get("SOLVER_CONFIG")
        if forced:
            p = Path(forced)
        else:
            here = Path(__file__).parent
            local = here / "config.local.yaml"
            p = local if local.exists() else here / "config.yaml"

    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with open(p) as f:
        cfg = yaml.safe_load(f)

    # 2. Env var overrides
    if os.getenv("CAPTCHA_ROUTER_URL"):
        cfg["router_url"] = os.environ["CAPTCHA_ROUTER_URL"]
    if os.getenv("CAPTCHA_ROUTER_KEY") is not None:
        cfg["router_key"] = os.environ["CAPTCHA_ROUTER_KEY"]
    if os.getenv("CAPTCHA_PRIMARY"):
        cfg.setdefault("models", {})["primary"] = os.environ["CAPTCHA_PRIMARY"]
    if os.getenv("CAPTCHA_FALLBACK"):
        cfg.setdefault("models", {})["fallback"] = [
            m.strip() for m in os.environ["CAPTCHA_FALLBACK"].split(",") if m.strip()
        ]
    if os.getenv("CAPTCHA_TIMEOUT_S"):
        cfg["timeout_s"] = int(os.environ["CAPTCHA_TIMEOUT_S"])
    return cfg


def _validate_config(cfg: dict) -> None:
    """Refuse to start with <PLACEHOLDER> values. Clear error beats httpx noise."""
    problems = []
    url = cfg.get("router_url", "")
    key = cfg.get("router_key", "")
    primary = (cfg.get("models") or {}).get("primary", "")

    if not url or url.startswith("<") and url.endswith(">"):
        problems.append("router_url is still a <PLACEHOLDER> — set it in config.yaml")
    if not key or (key.startswith("<") and key.endswith(">")):
        problems.append("router_key is still a <PLACEHOLDER> — set it in config.yaml (use 'EMPTY' for local servers that ignore auth)")
    if not primary or (primary.startswith("<") and primary.endswith(">")):
        problems.append("models.primary is still a <PLACEHOLDER> — pick a vision-capable model your endpoint serves")
    if problems:
        msg = "config.yaml is a TEMPLATE — fix before starting:\n  - " + "\n  - ".join(problems)
        raise SystemExit(msg)


CFG = load_config()
_validate_config(CFG)
LOG = logging.getLogger("captcha-llm")
logging.basicConfig(
    level=CFG.get("log_level", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# prompt helpers
# ---------------------------------------------------------------------------

PROMPTS: dict[str, str] = CFG["prompts"]


def build_prompt(captcha_type: str, hint: str = "") -> tuple[str, str]:
    """Return (system_prompt, user_text) for the given captcha type."""
    sys_prompt = PROMPTS.get(captcha_type, PROMPTS["image_text"])
    if hint:
        user_text = f"Context/hint: {hint}\n\nImage follows. Reply ONLY with the answer."
    else:
        user_text = "Image follows. Reply ONLY with the answer."
    return sys_prompt.strip(), user_text


# ---------------------------------------------------------------------------
# hermes-cli backend (default since v1.3)
# ---------------------------------------------------------------------------
# Slower than the HTTP router (30-60s typical) but **inherits the user's
# hermes config** — whatever model/provider is set in ~/.hermes/config.yaml
# gets used automatically. No router URL, API key, or model name to manage
# per request. Good for low-volume / debug / "I just want it to work" use
# cases. Pass use_hermes_cli=False to fall back to the HTTP router chain.

def solve_with_hermes_cli(
    image: str,
    captcha_type: str = "image_text",
    hint: str = "",
    *,
    timeout_s: float = 180.0,
) -> RouterSolveResult:
    """Spawn `hermes chat -q --image <tmp> -Q` and parse the answer.

    Inherits the user's current hermes config (model, provider, YOLO mode,
    etc.). Returns a RouterSolveResult compatible with the HTTP backend.
    """
    sys_p, user_text = build_prompt(captcha_type, hint)

    # Materialize image to a temp PNG file
    if _is_http_url(image):
        import httpx as _httpx
        r = _httpx.get(image, timeout=30)
        r.raise_for_status()
        raw_bytes = r.content
    else:
        b64 = _strip_data_url(image)
        raw_bytes = base64.b64decode(b64)

    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="captcha_")
    try:
        os.write(fd, raw_bytes)
        os.close(fd)

        # Build a single combined query: system prompt first, then user text.
        # hermes chat -q is one-shot, so we collapse the two-prompt structure.
        query = f"{sys_p}\n\n{user_text}"

        t0 = time.time()
        try:
            proc = subprocess.run(
                ["timplexz", "chat", "-q", query, "--image", tmp_path, "-Q"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"hermes chat -q timed out after {timeout_s}s")
        elapsed = time.time() - t0

        if proc.returncode != 0:
            raise RuntimeError(
                f"hermes chat -q rc={proc.returncode}: {proc.stderr[:300]}"
            )

        raw_text = proc.stdout.strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    answer = post_process_answer(raw_text, captcha_type)
    return RouterSolveResult(
        answer=answer,
        raw=raw_text,
        model="hermes-cli",  # actual model is whatever hermes is configured for
        elapsed_s=elapsed,
        tokens=0,             # subprocess doesn't expose token counts
        type=captcha_type,
        source="hermes_cli",
    )


# ---------------------------------------------------------------------------
# post-processor
# ---------------------------------------------------------------------------
# All prompts end with `ANSWER: <value>` on the last line. We extract from
# that marker — robust against thinking-mode reasoning leakage, truncation
# (the marker survives because it's at the END), and chatty models.
#
# The fallback chain for each captcha type:
#   1. Last `ANSWER: <value>` line
#   2. JSON array (for hCaptcha/recaptcha grid)
#   3. JSON object (for object_select bounding boxes)
#   4. Type-specific: math → bare number, text → last short alphanumeric token
#   5. If all else fails, the cleaned raw content

_ANSWER_RE = re.compile(r"^\s*ANSWER\s*:\s*(.+?)\s*$", re.M | re.I)
_JSON_ARR_RE = re.compile(r"\[\s*[\d\.\,\s]+\s*\]")
_JSON_OBJ_RE = re.compile(r"\{\s*[\d\.\,\s\[\]]+\s*\}")
_BARE_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")
# Cap how long a "text" answer can be — anything longer is probably reasoning.
_TEXT_ANSWER_MAX_LEN = 16


def _last_answer_marker(text: str) -> str | None:
    """Find the LAST `ANSWER: <value>` line. Returns value or None."""
    matches = _ANSWER_RE.findall(text)
    if matches:
        return matches[-1].strip()
    return None


def _first_json_array(text: str) -> str | None:
    """Return the first JSON-looking array substring, or None."""
    m = _JSON_ARR_RE.search(text)
    return m.group(0) if m else None


def _first_json_object(text: str) -> str | None:
    """Return the first JSON-looking object substring, or None."""
    m = _JSON_OBJ_RE.search(text)
    return m.group(0) if m else None


def _normalize_grid_cells(text: str) -> str | None:
    """Return a JSON array string for grid-cell answers, if possible."""
    arr = _first_json_array(text)
    if arr:
        return arr

    nums = re.findall(r"\b\d+\b", text)
    if not nums:
        return None
    return json.dumps([int(n) for n in nums])


def _extract_math(text: str) -> str | None:
    """Pick the first bare number in the text — usually the final answer."""
    m = _BARE_NUM_RE.search(text)
    return m.group(0) if m else None


def _extract_text_token(text: str) -> str | None:
    """Find a plausible captcha token: 3-16 alnum chars, last match wins."""
    tokens = _ALNUM_RE.findall(text)
    if not tokens:
        return None
    # Prefer the last token in any `ANSWER:` line, else the last short token.
    candidates = [t for t in tokens if 3 <= len(t) <= _TEXT_ANSWER_MAX_LEN]
    if candidates:
        return candidates[-1]
    return tokens[-1]


def post_process_answer(raw: str, captcha_type: str) -> str:
    """Extract a clean final answer from a model response.

    Falls through multiple strategies so the solver is robust against:
      - thinking-mode leakage (mimo, kimi, etc.)
      - truncation (finish_reason: length)
      - chatty models that add prose around the answer
      - JSON formatting drift (extra spaces, single vs double quotes)
    """
    if not raw or not raw.strip():
        return ""

    text = raw.strip()

    # Strategy 1: explicit `ANSWER:` marker (preferred, works for ALL types)
    marker = _last_answer_marker(text)
    if marker:
        # If the marker is a JSON array/object, return it as-is.
        if marker.startswith("[") and marker.endswith("]"):
            return marker
        if marker.startswith("{") and marker.endswith("}"):
            return marker
        if captcha_type in ("hcaptcha_image", "recaptcha_v2"):
            cells = _normalize_grid_cells(marker)
            return cells if cells is not None else marker
        # Otherwise: clean by captcha type
        if captcha_type == "math":
            num = _BARE_NUM_RE.search(marker)
            return num.group(0) if num else marker
        if captcha_type in ("image_text",):
            # Strip whitespace, keep visible chars
            cleaned = re.sub(r"\s+", "", marker)
            return cleaned
        # default: return marker as-is
        return marker

    # Strategy 2: type-specific fallbacks (no marker found)
    if captcha_type in ("hcaptcha_image", "recaptcha_v2"):
        cells = _normalize_grid_cells(text)
        if cells:
            return cells
        # fall through

    if captcha_type == "object_select":
        # Bounding boxes: look for array of arrays [[x,y,w,h], ...]
        m = re.search(r"\[\s*\[[\d\.\,\s]+\](?:\s*,\s*\[[\d\.\,\s]+\])*\s*\]", text)
        if m:
            return m.group(0)
        # fall through

    if captcha_type == "math":
        num = _extract_math(text)
        if num:
            return num
        # fall through

    if captcha_type == "image_text":
        token = _extract_text_token(text)
        if token:
            return token
        # fall through

    # Strategy 3: last-ditch — return raw stripped
    return text


# ---------------------------------------------------------------------------
# OCR backends (hybrid: OCR first for text captcha, LLM as fallback)
# ---------------------------------------------------------------------------
# We try Tesseract FIRST for image_text because:
#   - free, no API call
#   - very fast (~50-200ms)
#   - good for clean / low-noise text captchas (often >80% accuracy)
# If OCR returns a low-confidence result (too long, non-alnum, empty), we
# fall back to the LLM (which handles math, grid, and very-noisy text better
# via reasoning).
#
# To plug in a different OCR backend, set `ocr.backend` in config.yaml.
# Currently supported: "tesseract" (default), "easyocr", "none" (skip OCR).

import tempfile


def _ocr_tesseract(image_b64_or_url: str, captcha_type: str) -> tuple[str | None, float]:
    """Run Tesseract OCR on a base64 image. Returns (text, confidence 0-100).

    Uses image_to_data() for real per-word confidence — not just a length
    heuristic. Tesseract can confidently return garbage on stylized captchas,
    so the confidence check is the gate.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        LOG.warning("pytesseract/PIL not available, skipping OCR")
        return None, 0.0

    if _is_http_url(image_b64_or_url):
        LOG.info("OCR skipped for HTTP image URL; falling back to LLM")
        return None, 0.0

    b64 = _strip_data_url(image_b64_or_url)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))

    # Preprocess: scale up + grayscale + binarize (helps a lot for tiny text)
    img = img.convert("L")
    scale = CFG.get("ocr", {}).get("scale", 3)
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    threshold = CFG.get("ocr", {}).get("threshold", 160)
    img = img.point(lambda p: 0 if p < threshold else 255)

    psm = CFG.get("ocr", {}).get("psm", 7)  # 7 = single line, 8 = single word
    whitelist = CFG.get("ocr", {}).get("whitelist", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    config = f"--psm {psm} -c tessedit_char_whitelist={whitelist}"

    try:
        # image_to_data returns per-word confidence (-1 = no confidence for symbol)
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
    except Exception as e:
        LOG.warning(f"tesseract OCR failed: {e}")
        return None, 0.0

    # Pick the word with the highest confidence
    best_text = ""
    best_conf = -1.0
    n = len(data.get("text", []))
    for i in range(n):
        word = (data["text"][i] or "").strip()
        conf = float(data.get("conf", [0] * n)[i])
        if not word or conf < 0:
            continue
        if conf > best_conf:
            best_conf = conf
            best_text = word

    # Clean: keep alnum only, strip whitespace
    best_text = re.sub(r"\s+", "", best_text)
    if not best_text:
        return None, 0.0
    return best_text, best_conf


def _ocr_is_plausible(text: str, captcha_type: str) -> bool:
    """Sanity checks: length + charset fit the captcha type.

    This is a quick filter ON TOP of Tesseract's confidence — even if
    Tesseract is "confident", the result has to look like a real captcha
    token (not random punctuation, not a common English word, etc.).
    """
    if not text:
        return False
    if captcha_type == "math":
        return bool(_BARE_NUM_RE.fullmatch(text))
    if captcha_type == "image_text":
        # 3-8 chars, all alphanumeric, varied
        if not (3 <= len(text) <= 8):
            return False
        if not _ALNUM_RE.fullmatch(text):
            return False
        if len(set(text)) <= 1:  # not all same char
            return False
        return True
    return False


def try_ocr_first(image_b64_or_url: str, captcha_type: str) -> str | None:
    """If captcha type is amenable to OCR and OCR backend is enabled, try it.

    Returns the OCR result if BOTH:
      - Tesseract confidence is above threshold (default 60)
      - The result passes the plausibility check (length, charset, etc.)

    Else returns None and the caller should fall back to LLM.
    """
    if captcha_type not in ("image_text", "math"):
        return None
    backend = CFG.get("ocr", {}).get("backend", "tesseract")
    if backend == "none":
        return None
    if backend != "tesseract":
        LOG.warning(f"OCR backend '{backend}' not yet implemented, falling back to LLM")
        return None

    text, conf = _ocr_tesseract(image_b64_or_url, captcha_type)
    min_conf = CFG.get("ocr", {}).get("min_confidence_text" if captcha_type == "image_text" else "min_confidence_math", 50)

    if not _ocr_is_plausible(text, captcha_type):
        LOG.info(f"OCR implausible (text={text!r}, conf={conf}) — falling back to LLM")
        return None
    if conf < min_conf:
        LOG.info(f"OCR low-conf (text={text!r}, conf={conf:.0f} < {min_conf}) — falling back to LLM")
        return None

    LOG.info(f"OCR hit: '{text}' (tesseract conf={conf:.0f})")
    return text


# ---------------------------------------------------------------------------
# ai-router client
# ---------------------------------------------------------------------------

class RouterSolveResult(BaseModel):
    answer: str
    raw: str
    model: str
    elapsed_s: float
    tokens: int = 0
    type: str
    source: str = "llm"  # "llm" | "ocr" — which path produced the answer


def _strip_data_url(image: str) -> str:
    """If image is data:image/...;base64,XXX strip the prefix. Else return as-is."""
    m = re.match(r"^data:[^;]+;base64,(.+)$", image, re.S)
    if m:
        return m.group(1)
    return image


def _is_http_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _build_data_url(image_b64: str) -> str:
    """Wrap raw base64 in a data URL the chat API expects."""
    return f"data:image/png;base64,{image_b64}"


def call_router(
    model: str,
    image_b64_or_url: str,
    captcha_type: str,
    hint: str = "",
    timeout_s: float = 30.0,
    *,
    router_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> RouterSolveResult:
    """Send image to the configured LLM endpoint, return parsed answer + raw text.

    Optional per-call overrides (all keyword-only):
      - router_url:    override the LLM endpoint URL
      - api_key:       override the bearer token
      - max_tokens:    override CFG["solve"]["max_tokens"]
      - temperature:   override CFG["solve"]["temperature"]
    When None, falls back to CFG (config.yaml / env vars).
    """
    sys_p, user_text = build_prompt(captcha_type, hint)

    # If it's a URL, pass as URL; if it's a data URL, pass as data URL; if it's
    # raw base64, wrap it.
    if _is_http_url(image_b64_or_url):
        image_url = image_b64_or_url
    else:
        b64 = _strip_data_url(image_b64_or_url)
        image_url = _build_data_url(b64)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_p},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "max_tokens": max_tokens if max_tokens is not None else CFG["solve"]["max_tokens"],
        "temperature": temperature if temperature is not None else CFG["solve"]["temperature"],
    }

    headers = {
        "Authorization": f"Bearer {api_key or CFG['router_key']}",
        "Content-Type": "application/json",
    }
    url = router_url or CFG["router_url"]
    t0 = time.time()
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as e:
        raise RuntimeError(f"router transport error: {e}") from e
    elapsed = time.time() - t0

    if r.status_code != 200:
        raise RuntimeError(f"router HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    # Defensive parsing: many routers wrap content differently.
    # Some thinking-mode models (mimo-v2-omni, etc.) put the actual answer
    # in `reasoning_content` when the final `content` is empty/truncated
    # (finish_reason: "length").
    content = ""
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():
            # Fallback 1: reasoning_content (thinking models)
            rc = msg.get("reasoning_content") or ""
            if rc.strip():
                content = rc
            else:
                # Fallback 2: maybe the whole message IS the string
                content = msg if isinstance(msg, str) else json.dumps(msg)
    except (KeyError, IndexError, TypeError):
        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg if isinstance(msg, str) else json.dumps(msg)
    content = (content or "").strip()
    if not content:
        raise RuntimeError(f"empty content in response: {json.dumps(data)[:300]}")

    tokens = (data.get("usage") or {}).get("total_tokens", 0)
    # Post-process: extract the clean answer from the (possibly reasoning-leaked)
    # raw content. `raw` keeps the full model output for debugging.
    answer = post_process_answer(content, captcha_type)
    return RouterSolveResult(
        answer=answer,
        raw=content,
        model=model,
        elapsed_s=round(elapsed, 3),
        tokens=tokens,
        type=captcha_type,
    )


def solve_with_fallback(
    image: str,
    captcha_type: str = "image_text",
    hint: str = "",
    *,
    llm_override: Optional[Dict] = None,
    use_hermes_cli: bool = True,
) -> RouterSolveResult:
    """Solve captcha. Hybrid flow:

    1. If captcha type is amenable to OCR (image_text, math) and OCR backend
       is enabled, try OCR first. If confidence is high, return immediately.
    2. Otherwise, walk the LLM model chain. For each model:
         a. If the response looks like a reasoning leak, do ONE re-prompt
            with a "just give me the ANSWER line" reminder.
         b. If the re-prompt gives a clean answer, use that.
         c. Otherwise use the original response.

    Optional llm_override (keyword-only) lets you switch router/model/key
    per request without restarting the server. Supported keys:
      - router_url:    str, full /v1/chat/completions URL
      - api_key:       str, bearer token
      - model:         str, primary model name (overrides CFG["models"]["primary"])
      - fallback:      str or list[str], additional fallback models
      - max_tokens:    int
      - temperature:   float

    Optional use_hermes_cli (default True): when True, skip the LLM chain
    entirely and use `solve_with_hermes_cli()` (slower but inherits hermes
    config). Set to False to force the HTTP router chain.
    """
    # --- Phase 1: OCR (for text/math types only) ---
    ocr_result = try_ocr_first(image, captcha_type)
    if ocr_result is not None:
        return RouterSolveResult(
            answer=ocr_result,
            raw=ocr_result,
            model=f"ocr:{CFG.get('ocr', {}).get('backend', 'tesseract')}",
            elapsed_s=0.0,
            tokens=0,
            type=captcha_type,
            source="ocr",
        )

    # --- Phase 2a: hermes-cli backend (default, simpler) ---
    if use_hermes_cli:
        return solve_with_hermes_cli(image, captcha_type, hint)

    # --- Phase 2b: LLM chain (HTTP router, requires llm_override or env config) ---
    ov = llm_override or {}
    _primary = ov.get("model") or CFG["models"]["primary"]
    _fallback = ov.get("fallback")
    if _fallback is None:
        _fallback = CFG["models"].get("fallback") or []
    elif isinstance(_fallback, str):
        _fallback = [_fallback] if _fallback else []
    chain = [_primary] + list(_fallback)
    common_kwargs = {
        "router_url": ov.get("router_url"),
        "api_key":    ov.get("api_key"),
        "max_tokens": ov.get("max_tokens"),
        "temperature": ov.get("temperature"),
    }
    last_err = None
    for model in chain:
        try:
            LOG.info(f"trying model={model} type={captcha_type}")
            result = call_router(model, image, captcha_type, hint, CFG["solve"]["timeout_s"], **common_kwargs)
            result.source = "llm"
            # If the response is too long (likely reasoning leak) AND we
            # didn't find an ANSWER marker, do one cheap re-prompt with the
            # SAME model+image but a stricter "give me just the answer" frame.
            if _looks_like_reasoning_leak(result.raw, result.answer):
                LOG.info(f"  model={model} leaked reasoning, re-prompting for clean answer")
                result2 = call_router(
                    model, image, captcha_type,
                    hint + "\n\n[Reminder: reply with ONLY the ANSWER: line, nothing else.]",
                    CFG["solve"]["timeout_s"],
                    **common_kwargs,
                )
                result2.source = "llm"
                # If the re-prompt got a clean answer, prefer it
                if not _looks_like_reasoning_leak(result2.raw, result2.answer):
                    return result2
            return result
        except Exception as e:
            last_err = e
            LOG.warning(f"model={model} failed: {e}")
    raise RuntimeError(f"all models failed. last_err={last_err}")


def _looks_like_reasoning_leak(raw: str, extracted: str) -> bool:
    """Heuristic: did the model leak its thinking instead of answering?

    Signals:
      - raw is long (>300 chars) but extracted answer is short → suspect
      - extracted is a common English word → suspect
      - raw contains "Thinking Process:" or "1. **Analyze" → suspect
    """
    if not raw or not extracted:
        return True
    # Common reasoning tokens that the post-processor might grab by mistake
    bad_words = {
        "and", "the", "for", "with", "this", "that", "have", "from",
        "are", "was", "were", "you", "your", "character", "characters",
        "image", "shown", "appears", "again", "check", "looks", "looks like",
        "first", "second", "third", "last", "middle", "think", "thinking",
        "answer", "process", "analyze", "image", "characters", "KCoL", "character",
    }
    if extracted.lower().strip() in bad_words:
        return True
    # Long raw + short extracted → likely reasoning
    if len(raw) > 300 and len(extracted) < 6:
        return True
    # Contains "Thinking Process:" → reasoning leak
    if "Thinking Process" in raw or "thinking process" in raw.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="global-captcha /solve", version="1.3.1")


class SolveReq(BaseModel):
    image: str = Field(..., description="base64 (with or without data URL prefix) or http(s) URL")
    type: str = Field("image_text", description="image_text | hcaptcha_image | recaptcha_v2 | math | object_select")
    hint: str = Field("", description="optional context, e.g. challenge question text")


class SolveResp(BaseModel):
    answer: str
    model: str
    elapsed_s: float
    tokens: int
    type: str


@app.get("/health")
def health():
    return {
        "ok": True,
        "router": CFG["router_url"],
        "primary_model": CFG["models"]["primary"],
        "fallbacks": CFG["models"].get("fallback", []),
    }


@app.get("/models")
def models():
    return {
        "primary": CFG["models"]["primary"],
        "fallback": CFG["models"].get("fallback", []),
        "types": list(PROMPTS.keys()),
    }


@app.post("/solve", response_model=SolveResp)
def solve(req: SolveReq):
    if not req.image:
        raise HTTPException(400, "image field required")
    if req.type not in PROMPTS:
        raise HTTPException(400, f"unknown type: {req.type}. valid: {list(PROMPTS.keys())}")
    # size check (only meaningful for base64)
    if not _is_http_url(req.image):
        b64 = _strip_data_url(req.image)
        approx_bytes = len(b64) * 3 // 4
        if approx_bytes > CFG["solve"]["max_image_bytes"]:
            raise HTTPException(413, f"image too large ({approx_bytes} bytes)")
    try:
        res = solve_with_fallback(req.image, req.type, req.hint)
    except Exception as e:
        raise HTTPException(502, f"solve failed: {e}")
    return SolveResp(**res.model_dump())


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _read_image_as_base64(path: str) -> str:
    p = Path(path)
    if not p.exists():
        sys.exit(f"image not found: {path}")
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode()
    return _build_data_url(b64)


def cli():
    ap = argparse.ArgumentParser(description="global-captcha /solve CLI")
    ap.add_argument("--test", metavar="IMAGE", help="image file path to solve (CLI mode)")
    ap.add_argument("--type", default="image_text", help="captcha type (default: image_text)")
    ap.add_argument("--model", default=None, help="override primary model")
    ap.add_argument("--hint", default="", help="optional hint text")
    args = ap.parse_args()

    if args.model:
        CFG["models"]["primary"] = args.model

    if args.test:
        img = _read_image_as_base64(args.test)
        try:
            res = solve_with_fallback(img, args.type, args.hint)
        except Exception as e:
            sys.exit(f"solve failed: {e}")
        print(json.dumps(res.model_dump(), indent=2))
        return

    # else: server mode
    import uvicorn
    host = CFG["server"]["host"]
    port = CFG["server"]["port"]
    LOG.info(f"starting global-captcha /solve on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level=CFG.get("log_level", "info").lower())


if __name__ == "__main__":
    cli()
