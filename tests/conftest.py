import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("AUTOSTART", "0")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="memebot-test-"))
os.environ.setdefault("DASHBOARD_USER", "luis")
os.environ.setdefault("DASHBOARD_PASSWORD", "geheim")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def store(tmp_path):
    from memebot.store import Store
    return Store(str(tmp_path / "t.sqlite"))
