import pytest
import aiohttp
from urllib.parse import urljoin
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

pytest_plugins = "aiohttp.pytest_plugin"


class E2ETestSettings(BaseSettings):
    """Settings for running end to end (E2E) test suite"""

    endpoint_url: AnyHttpUrl = "http://localhost:8000"

    model_config = SettingsConfigDict(env_prefix="E2E_")

    def with_destination(self, path: str) -> str:
        """Returns the url or the endpoint given a new path

        Args:
            path: URL path to be appended to the endpoint URL

        Returns:
            URL with new path appended
        """
        return urljoin(self.endpoint_url, path)


@pytest.fixture(scope="session")
def settings():
    """Fixture for supplying test settings throughout test session"""
    return E2ETestSettings()


@pytest.fixture()
async def http_session(loop, settings):
    """Fixture to provide an async object allowing to make requests

    Note that we need to depend on the "loop" fixture because this
    is an async (asyncio) test.
    """
    async with aiohttp.ClientSession(base_url=str(settings.endpoint_url)) as session:
        yield session
