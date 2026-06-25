import logging
from redis.asyncio import Redis
from settings import get_settings
from redis_entraid.cred_provider import create_from_default_azure_credential

_cfg = get_settings()


logger = logging.getLogger(__name__)


def build_redis_client(host: str) -> Redis:
    logger.info(f"REDIS: connecting host={host!r} port=6380 ssl=True")
    # cred = create_from_default_azure_credential(("https://redis.azure.com/.default",))
    common = dict(
        host=host,
        port=6380,
        ssl=True,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    access_key = _cfg.REDIS_ACCESS_KEY
    if access_key:
        logger.info("REDIS: using access-key auth")
        client = Redis(password=access_key, **common)
    else:
        logger.info("REDIS: using Extra managed-identity auth")
        cred = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )
        client = Redis(credential_provider=cred, **common)
    try:
        client.ping()
        logger.info("REDIS: ping OK - connected")
    except Exception as e:
        logger.error(f"REDIS: connect FAILED -> {type(e).__name__}: {e}")
        raise
    return client
