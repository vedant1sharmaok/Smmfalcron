"""Integration-test environment. Needs:  pip install -r requirements-dev.txt

Settings are read from the environment at import time, so everything is set BEFORE app imports.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="falaron-test-")
os.environ.update(
    {
        "APP_ENV": "development",
        "BOT_TOKEN": "123456789:TESTTOKENTESTTOKENTESTTOKENTEST",
        "OWNER_TELEGRAM_ID": "1",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP}/test.db",
        "PAYMENT_GATEWAY": "razorpay",
        "ENABLE_MOCK_PAYMENTS": "false",
        "RAZORPAY_KEY_ID": "rzp_test_x",
        "RAZORPAY_KEY_SECRET": "secret",
        "RAZORPAY_WEBHOOK_SECRET": "whsec_test",
        "SECRET_KEY": "k" * 44,
        "WELCOME_BONUS_PAISE": "0",
    }
)

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture
async def db():
    from app.config import get_settings
    from app.db import dispose_engine, init_db

    get_settings.cache_clear()
    await dispose_engine()
    path = os.path.join(_TMP, "test.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    await init_db()
    yield
    await dispose_engine()
