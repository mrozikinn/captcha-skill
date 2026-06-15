#!/usr/bin/env python3
"""
Global CAPTCHA Solver Launcher — runs embedded FastAPI server.
"""
import json, os, time
from pathlib import Path
import importlib.util

SCRIPT_DIR = Path(__file__).parent

# Load config
config_path = SCRIPT_DIR / "captcha_solver_config.json"
if config_path.exists():
    try:
        config = json.loads(config_path.read_text())
    except Exception as e:
        print(f"[!] Failed to load config: {e}")
        config = {}
else:
    config = {}

# Load captcha_solver module
spec = importlib.util.spec_from_file_location("captcha_solver", str(SCRIPT_DIR / "captcha_solver.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Get config values with defaults
host = config.get('host', '0.0.0.0')
port = config.get('port', 8001)
log_level = config.get('debug', False) and 'debug' or 'info'

# Create server instance
server = module.ClearanceAPIServer(
    headless=config.get('headless', True),
    thread=config.get('thread', 1),
    page_count=config.get('page_count', 1),
    proxy_support=config.get('proxy_support', False),
    proxy_file=config.get('proxy_file', 'proxies.txt'),
    cleanup_interval_minutes=config.get('cleanup_interval_minutes', 10),
)

print(f"Starting CAPTCHA Solver on {host}:{port} ({log_level} level)...")

# Run with uvicorn
import uvicorn
uvicorn.run(server.app, host=host, port=port, log_level=log_level)
