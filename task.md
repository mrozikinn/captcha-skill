```text
                         Global CAPTCHA Solver

Client
  |
  |  Async browser challenges
  |  GET /turnstile?url=...&sitekey=...
  |  GET /recaptchaV3?url=...&sitekey=...
  |  GET /clearance?url=...
  |  GET /aws-token?url=...
  |
  v
+-----------------------------+
| FastAPI: captcha_solver.py  |
| - validates request         |
| - creates task_id           |
| - returns 202 Accepted      |
+-------------+---------------+
              |
              v
+-----------------------------+
| Page Pool Manager           |
| - thread x page_count slots |
| - proxy rotation optional   |
| - periodic cleanup          |
+-------------+---------------+
              |
              v
+-----------------------------+
| Camoufox Browser Context    |
| - stealth profile           |
| - target origin/page        |
| - widget/script injection   |
+-------------+---------------+
              |
              v
    +---------+----------+----------------+
    |                    |                |
    v                    v                v
+-----------+    +---------------+   +----------------+
| Turnstile |    | reCAPTCHA v3  |   | Clearance/WAF  |
| inject    |    | load api.js   |   | navigate page  |
| click     |    | execute()     |   | wait challenge |
| poll token|    | get token     |   | collect cookie |
+-----+-----+    +-------+-------+   +--------+-------+
      |                  |                    |
      +------------------+--------------------+
                         |
                         v
              +---------------------+
              | self.results[task]  |
              | success/error state |
              +----------+----------+
                         |
                         v
Client polls GET /result?id=<task_id>
  |
  +--> 202 still processing
  +--> 200 token/cookie
  +--> 408 timeout
  +--> 422 solve error


Client
  |
  |  Image CAPTCHA / direct solve
  |  POST /solve
  |  {
  |    "image": "base64 | data URL | http(s) URL",
  |    "type": "image_text | math | hcaptcha_image | recaptcha_v2 | object_select",
  |    "hint": "...",
  |    "use_hermes_cli": false,
  |    "router_url": ".../v1/chat/completions",
  |    "api_key": "..."
  |  }
  |
  v
+-----------------------------+
| FastAPI: /solve route       |
| - loads solver.py lazily    |
| - passes per-request LLM    |
|   overrides when provided   |
+-------------+---------------+
              |
              v
+-----------------------------+
| solver.py                   |
| - config.yaml template      |
| - config.local.yaml local   |
| - env var overrides         |
+-------------+---------------+
              |
              v
    +---------+--------------------+
    |                              |
    v                              v
+------------------+        +-----------------------+
| OCR first        |        | LLM direct            |
| image_text/math  |        | hcaptcha_image        |
| Tesseract if     |        | recaptcha_v2          |
| installed/high   |        | object_select         |
| confidence       |        +-----------+-----------+
+--------+---------+                    |
         |                              |
         | high confidence              | OCR skipped/failed
         v                              v
  +-------------+              +----------------------+
  | Return OCR  |              | Backend selection    |
  | answer      |              | - hermes-cli default |
  +-------------+              | - HTTP router when   |
                               |   use_hermes_cli=false
                               +----------+-----------+
                                          |
                                          v
                         +-------------------------------+
                         | HTTP router / OpenAI-compatible|
                         | POST /v1/chat/completions     |
                         | model fallback chain          |
                         +---------------+---------------+
                                         |
                                         v
                         +-------------------------------+
                         | post_process_answer()         |
                         | - extracts final answer       |
                         | - normalizes grid cells       |
                         |   "1, 8" -> "[1, 8]"          |
                         +---------------+---------------+
                                         |
                                         v
                         HTTP 200 JSON response
                         {
                           "answer": "...",
                           "model": "...",
                           "elapsed_s": 0.0,
                           "tokens": 0,
                           "type": "..."
                         }
```
