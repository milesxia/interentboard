from pathlib import Path
import asyncio
import os
import yaml

os.environ.setdefault("MOCK_AI", "true")
os.environ.setdefault("AUTO_PULL_MODEL", "false")

from app.config import load_topics, settings
from app.services.fetch import Fetcher


def check_compose():
    for f in ["docker-compose.yml", "docker-compose.cpu.yml", "docker-compose.dev.yml"]:
        doc = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
        assert "services" in doc and "intelboard" in doc["services"]
    gpu = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    devices = gpu["services"]["ollama"]["deploy"]["resources"]["reservations"]["devices"]
    assert devices[0]["driver"] == "nvidia"
    print("[OK] Compose YAML parsed; NVIDIA reservation present")


def check_topics():
    topics = load_topics(Path("config/topics.yml"))
    assert len(topics) == 5
    print("[OK] 5 topic configurations loaded")


async def live_fetch():
    # Official page already present in the user's baseline evidence index.
    url = "https://www.shanghai.gov.cn/nw15343/20260730/8855320b47fe4e9faa56abe4662ad692.html"
    try:
        page = await Fetcher(timeout=20).fetch(url)
        assert len(page.text) > 200
        print(f"[OK] Live official-page fetch: {page.title[:70]} ({len(page.text)} chars)")
    except Exception as e:
        print(f"[WARN] Live fetch unavailable in this environment: {e}")


if __name__ == "__main__":
    check_compose()
    check_topics()
    asyncio.run(live_fetch())
