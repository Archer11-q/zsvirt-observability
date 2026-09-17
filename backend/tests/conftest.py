"""pytest 共享夹具。"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
