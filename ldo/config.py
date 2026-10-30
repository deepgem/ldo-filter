"""配置加载：config.json + .env（.env 只覆盖密钥类字段）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略 # 注释与空行，支持行尾注释前的内容。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    env = load_env()
    # 环境变量优先（真实环境变量 > .env > config.json）
    for key, envname in (
        ("base_url", "LLM_BASE_URL"),
        ("model", "LLM_MODEL"),
    ):
        if os.environ.get(envname) or env.get(envname):
            cfg["llm"][key] = os.environ.get(envname) or env[envname]

    cfg["llm"]["api_key"] = os.environ.get("LLM_API_KEY") or env.get("LLM_API_KEY", "")
    if "jev" in cfg:
        cfg["jev"]["api_key"] = (
            os.environ.get("TYPESAFE_API_KEY") or env.get("TYPESAFE_API_KEY", "")
        )
    cfg["telegram"] = {
        "bot_token": os.environ.get("TG_BOT_TOKEN") or env.get("TG_BOT_TOKEN", ""),
        "chat_id": os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", ""),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg["db_path"] = str(DATA_DIR / "ldo.db")
    return cfg
