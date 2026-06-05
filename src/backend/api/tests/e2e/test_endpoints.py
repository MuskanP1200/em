import logging
from typing import Any
from urllib.parse import urlencode

import pytest

logger = logging.getLogger(__name__)


def query_param_idfn(params):
    if isinstance(params, dict):
        return "?" + urlencode(params)


async def validate_response_in_json_format(resp, status_code: int = 200) -> Any:
    """Utilitary to make sure response is valid (JSON) and has expected status code

    Args:
        resp: The AIOHTTP response with the body not consumed
        status_code: The expected status code

    Raises:
        AssertionError if status code of response is not the expected value
    """
    logger.info(resp.url)
    assert (
        resp.status == status_code
    )  # nosec: B101 -- pytest assertion in test; safe because this is test code
    assert (
        resp.content_type == "application/json"
    )  # nosec: B101 -- pytest assertion in test; safe because this is test code
    js = await resp.json()
    logger.debug(js)
    return js


@pytest.mark.parametrize(
    ["url", "payload"],
    [
        ("/get_response/", {"prompt": "123456", "vehicle_info": "some vehicle info"}),
    ],
    ids=query_param_idfn,
)
async def test_api_get_response(http_session, url, payload):
    async with http_session.post(url, json=payload) as resp:
        js = await validate_response_in_json_format(resp)
        assert isinstance(
            js, dict
        )  # nosec: B101 -- pytest assertion in test; safe because this is test code
