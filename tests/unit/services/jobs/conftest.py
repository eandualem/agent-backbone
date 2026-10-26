import pytest

from tests.support import register_config_agents


@pytest.fixture
async def db(db, config):
    await register_config_agents(db, config)
    yield db
