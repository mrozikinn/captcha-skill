---
name: global-captcha
description: Use when a target site requires Cloudflare Turnstile, Google reCAPTCHA v3/v2, CF WAF clearance, AWS WAF token bypass, or image-based CAPTCHA solving (hCaptcha grid, reCAPTCHA v2 image, text/math, object detection). Self-hosted FastAPI server on port 8001 with embedded Camoufox browser pool + optional LLM backend for image challenges. Returns tokens/cookies via simple HTTP GET/POST.
version: 1.3.1
author: Timplexz
license: MIT
metadata:
  timplexz:
    tags: [captcha, turnstile, recaptcha, cloudflare, aws-waf, bypass, browser-pool]
    related_skills: [turnstile-bypass-playwright, cloudflare-bypass-camoufox-fallback, proxy-ip-rotation]
---

# Global CAPTCHA Solver

Self-hosted CAPTCHA bypass server. Solves the four most common challenge types (Cloudflare Turnstile, Google reCAPTCHA v3, Cloudflare WAF clearance, AWS WAF) by running an embedded Camoufox/Playwright browser pool. No external API keys, no per-solve cost.

> **Path convention:** `<skill-dir>` = wherever this skill is installed. All paths in this SKILL.md are relative to `<skill-dir>` unless stated otherwise.

## When to Use

- A target site's HTML/JS contains a Cloudflare Turnstile widget and a `cf-turnstile` sitekey is visible in the page source.
- A target is gated behind Cloudflare WAF and returns 403 with a `cf-mitigated: challenge` header.
- An AWS CloudFront distribution issues an `aws-waf-token` cookie challenge.
- A target renders Google reCAPTCHA v3 (invisible score-based) and the form submit needs a `g-recaptcha-response` token.

Don't use for:
- Image-based CAPTCHAs (hCaptcha image challenges, reCAPTCHA v2 image grid) — this skill only covers the script-based variants.
- Sites that require logged-in session cookies after challenge solve (chain the returned cookies with your session manager).
- Long-running stealth browsing — this is a one-shot token factory, not a scraping proxy.

## File Layout

```
<skill-dir>/
├── SKILL.md                  this file (agent-facing, triggers on description match)
├── README.md                 human-facing setup + API reference
├── captcha-solver.service    systemd unit template (edit paths before installing)
├── captcha_solver.py         main FastAPI server (ClearanceAPIServer class) + /solve route
├── solver.py                 /solve backend: hybrid OCR + LLM (hermes-cli default, HTTP router fallback)
├── run_captcha_solver.py     launcher: reads config, imports server module, starts uvicorn
├── captcha_solver_config.json  runtime config (port, threads, headless, proxy, debug)
├── config.yaml               /solve backend config (TEMPLATE — no real keys)
├── config.local.yaml         /solve runtime overrides (gitignored, real values)
├── .env.example              env var override examples for /solve
├── .gitignore                excludes config.local.yaml, .env, debug_logs/, __pycache__/
├── samples/                  test captcha images (math, hcaptcha grid, text, recaptcha_v2)
└── debug_logs/               created on demand, holds failed-solve screenshots
```

The launcher auto-discovers its own directory via `Path(__file__).parent` — no environment variables needed for the default install. Override the config path with `CAPTCHA_SOLVER_CONFIG=/path/to/other.json` for multi-instance setups.

## Endpoints

All endpoints live on `http://localhost:8001`. Interactive docs at `/docs` (Swagger UI).

Security note: keep this service bound to localhost or behind your own authenticated proxy/firewall. The API accepts caller-supplied target URLs and image URLs, so exposing it publicly without access control can let other users spend your browser/LLM resources or make the host fetch internal/private network URLs.

### 1. Turnstile — `GET /turnstile`

Returns a one-shot Turnstile token valid for ~300s on the target origin.

| Param  | Required | Description |
|--------|----------|-------------|
| url    | yes      | Target URL whose origin should appear in the solve page |
| sitekey| yes      | `0x4AAA...` sitekey from page source |
| action | no       | Turnstile action (default: empty) |
| cdata  | no       | Custom data blob from `data-cdata` attribute |

Response (async): `{"task_id": "uuid", "status": "accepted"}`. Poll `/result?id=<task_id>` to get the token.

### 2. CF Clearance — `GET /clearance`

Opens the target URL in a fresh browser context, solves the Cloudflare interstitial, returns the `cf_clearance` cookie. Cookie lifetime is whatever Cloudflare set (usually 30min-24h).

| Param   | Required | Description |
|---------|----------|-------------|
| url     | yes      | Target URL (any path on the protected domain) |
| timeout | no       | Max seconds to wait for challenge to clear (default: 30) |

Response includes `cookies: {cf_clearance: "..."}`. Reuse the cookie in subsequent requests to that domain.

### 3. AWS WAF Token — `GET /aws-token`

Solves the AWS WAF challenge and returns the `aws-waf-token` cookie. Same shape as `/clearance`.

### 4. reCAPTCHA v3 — `GET/POST /recaptchaV3`

Invisible score-based challenge. Returns a `g-recaptcha-response` token suitable for direct POST to the target form.

| Param  | Required | Description |
|--------|----------|-------------|
| url    | yes      | Target URL |
| sitekey| yes      | `6Lc...` sitekey |
| action | no       | reCAPTCHA action (e.g. `login`, `submit`) |

### 5. Image CAPTCHA — `POST /solve`

Solve an image-based CAPTCHA. Two backends available — pick whichever fits.

**Quick decision:**
- **just want it to work, no setup** → leave `use_hermes_cli` unset (default `true`). The solver uses your hermes config (model, provider, API key) automatically. Slower (30-100s/req) because it spawns `hermes chat -q` per call.
- **need speed, or want to pin a specific model** → set `use_hermes_cli: false`. Faster (2-10s/req) but you supply the router URL + API key via env vars or per-request override.

**API key handling (auto):**
- `hermes-cli` mode reads whatever API key/model/provider is in `~/.hermes/config.yaml` — zero setup. If your hermes config uses an upstream like `axai` or `9router`, the key from there is used. You do NOT pass `api_key` in the request body.
- `HTTP router` mode (`use_hermes_cli: false`) needs an explicit `api_key` (or env var `CAPTCHA_ROUTER_KEY`). Without it, returns `401 Invalid API key`.

Body (JSON):
| Field         | Required | Description |
|---------------|----------|-------------|
| image         | yes      | base64-encoded image, data URL, or http(s) URL |
| type          | yes      | `image_text`, `math`, `hcaptcha_image`, `recaptcha_v2`, `object_select` |
| hint          | no       | hint string (e.g. "select all images containing a bicycle") |
| use_hermes_cli | no       | **default `true`**. `false` to use HTTP router chain instead |

For HTTP router mode (`use_hermes_cli: false`), additional override fields:
| Field         | Required | Description |
|---------------|----------|-------------|
| router_url    | no       | full `/v1/chat/completions` URL |
| api_key       | no       | bearer token (or set `CAPTCHA_ROUTER_KEY` env var) |
| model         | no       | primary model name |
| fallback      | no       | string OR list of strings |
| max_tokens    | no       | int |
| temperature   | no       | number |

When override fields are omitted/empty, they fall back to env vars /
config.local.yaml started with the server.

Response:
```json
{
  "answer": "13",
  "raw": "13",
  "model": "hermes-cli",       // or "mimo-v2-omni" in HTTP router mode
  "elapsed_s": 94.1,           // 30-100s for hermes-cli, 2-10s for HTTP
  "tokens": 0,                 // always 0 in hermes-cli mode
  "type": "math",
  "source": "hermes_cli"       // or "llm" or "ocr"
}
```

**Backend comparison:**

| backend      | speed      | api key | config needed | use when |
|--------------|------------|---------|---------------|----------|
| `hermes-cli` (default) | 30-100s/request | auto from `~/.hermes/config.yaml` | none | one-off, debug, "just works" |
| `HTTP router` (`use_hermes_cli: false`) | 2-10s/request | must supply (env var or per-request) | env vars or per-request override | batch, latency-sensitive, fixed model |

**Speed note:** `hermes-cli` speed depends on whatever model your hermes config points to. If your model is slow (e.g. a large local LLM), expect closer to 100s+. Swap to a faster model in hermes config, or use HTTP router mode.

```bash
# default — uses hermes chat -q, no config needed
curl -s -X POST http://localhost:8001/solve \\
  -H 'Content-Type: application/json' \\
  -d '{"image":"<b64>","type":"math"}'

# with hint
curl -s -X POST http://localhost:8001/solve \\
  -H 'Content-Type: application/json' \\
  -d '{"image":"<b64>","type":"hcaptcha_image","hint":"bicycles"}'

# force HTTP router for speed
curl -s -X POST http://localhost:8001/solve \\
  -H 'Content-Type: application/json' \\
  -d '{"image":"<b64>","type":"math","use_hermes_cli":false}'

# HTTP router with per-request model override
curl -s -X POST http://localhost:8001/solve \\
  -H 'Content-Type: application/json' \\
  -d '{
    "image": "<b64>",
    "type": "math",
    "use_hermes_cli": false,
    "router_url": "http://localhost:32128/v1/chat/completions",
    "api_key": "sk-...",
    "model": "mimo-v2-omni",
    "fallback": ["gemini-2.5-flash"],
    "max_tokens": 256,
    "temperature": 0.1
  }'
```

### 6. Result polling — `GET /result?id=<task_id>`

| Status      | Meaning |
|-------------|---------|
| `pending`   | Still solving, poll again |
| `processing`| Browser is on the challenge page |
| `success`   | `value` field contains token or cookie payload |
| `failure`   | `error` field contains diagnostic; check `headless`/`proxy_support`/`debug` |

## Configuration

Edit `captcha_solver_config.json` in place. All fields optional; defaults shown.

```json
{
  "headless": true,
  "thread": 5,
  "page_count": 3,
  "proxy_support": false,
  "proxy_file": "proxies.txt",
  "host": "0.0.0.0",
  "port": 8001,
  "debug": true,
  "cleanup_interval_minutes": 10
}
```

| Field | Effect |
|-------|--------|
| `headless` | `false` requires a DISPLAY (X server) — only for debugging browser-detected challenges |
| `thread` | Browser pool size; one browser per concurrent solve. 5 is a safe default for 4-core boxes |
| `page_count` | Reuse a single browser context for N solves before recycling (reduces fingerprint churn) |
| `proxy_support` | `true` reads one line per proxy from `proxy_file` (format: `http://user:pass@host:port`) |
| `debug` | Uvicorn log level; `true` = debug, `false` = info |
| `cleanup_interval_minutes` | Internal browser-context recycling interval (memory hygiene) |

Restart the server after any config change.

## Run / Lifecycle

### Quick start (foreground, manual)
```bash
cd <skill-dir>
python3 run_captcha_solver.py
```

### Production (systemd — recommended)

The bundled `captcha-solver.service` uses placeholder paths. **Edit them to match your install** before installing:

```bash
# 1. Edit captcha-solver.service — replace:
#    WORKING_DIRECTORY  → absolute path to <skill-dir>
#    PYTHON_BIN         → absolute path to python3 with camoufox installed

# 2. Install + enable
sudo cp captcha-solver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now captcha-solver
sudo systemctl status captcha-solver
```

### Verify it's up
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8001/docs   # expect 200
curl -s "http://localhost:8001/turnstile?url=https://example.com&sitekey=1x00000000000000000000AA"
# expect: {"task_id": "...", "status": "accepted"}
```

### Restart / stop
```bash
sudo systemctl restart captcha-solver
sudo systemctl stop captcha-solver
```

## Usage Example

```python
import requests, time

BASE = "http://localhost:8001"

# Submit Turnstile task
task = requests.get(
    f"{BASE}/turnstile",
    params={"url": "https://modelgate.app", "sitekey": "0x4AAAAAADgeWs3ppdYjDB2c"}
).json()
task_id = task["task_id"]

# Poll for result (max 2 minutes)
token = None
for _ in range(60):
    r = requests.get(f"{BASE}/result", params={"id": task_id}).json()
    if r.get("status") == "success":
        token = r["value"]
        break
    time.sleep(2)

# Use the token in your target form/header
headers = {"cf-turnstile-response": token}
```

## Common Pitfalls

1. **Port 8001 already in use.** Another process (often a leftover python from a previous start) holds the port. Fix: `sudo lsof -i :8001` then `kill -9 <pid>`. If using systemd, `systemctl restart captcha-solver` does this cleanly.
2. **Camoufox browser not installed.** First run downloads ~300MB of browser binaries to `~/.cache/camoufox/`. If the venv lacks network access or disk, the server crashes silently on first solve. Fix: `python3 -c "from camoufox.async_api import AsyncCamoufox"` to warm it up.
3. **`proxy_support: true` but no `proxies.txt`.** Server reads but finds empty file → silently uses no proxy. Fix: write at least one proxy line, or set `false`.
4. **Wrong sitekey.** Turnstile solver returns a token but the target rejects it. 99% of the time the sitekey is wrong (e.g. copied the widget class name instead of the `data-sitekey` attribute). Verify by grepping the page source.
5. **Stale `cf_clearance` cookie.** Cloudflare rotates them; don't reuse cookies across days. Re-solve on each session.
6. **Headless detected.** Some CF configurations detect headless browsers. Set `headless: false` and run on a host with X (or use Xvfb). Alternatively chain with `cloudflare-bypass-camoufox-fallback` skill.
7. **`/result` returns `failure` with no `error` field.** Browser timed out before the challenge rendered. Increase the `timeout` param on the submit endpoint (default 30s).
8. **Server log shows "Attribute 'app' not found in module 'captcha_solver'".** You started uvicorn from the wrong cwd. Always `cd <skill-dir>` or use the launcher script.
9. **systemd unit fails to load.** You didn't replace the `WORKING_DIRECTORY` / `PYTHON_BIN` placeholders in `captcha-solver.service`. `systemctl status captcha-solver` will show the actual error.

## Verification Checklist

- [ ] `curl http://localhost:8001/docs` returns 200
- [ ] `/turnstile` submit returns `{"task_id": "...", "status": "accepted"}` (not 500)
- [ ] `/result?id=<task_id>` reaches `success` within 60s for a known-good Turnstile (e.g. Cloudflare's demo sitekey `1x00000000000000000000AA`)
- [ ] After config edit, `systemctl restart captcha-solver` succeeds with no errors in `journalctl -u captcha-solver -n 50`
- [ ] `proxy_support: true` actually routes through a proxy — verify by checking the solver's outbound IP differs from the host IP (`curl ifconfig.me` from inside the browser context)
- [ ] If distributing the skill: no absolute paths hardcoded in `SKILL.md` / `README.md` / `captcha-solver.service` (use `<skill-dir>` placeholders and edit-on-install)
