from cashews import cache, Cache as CacheConfigurator  # noqa: F401

from ..settings import get_settings


def initialize_caching(Settings):
    host = Settings.REDIS_HOST
    port = Settings.REDIS_PORT
    password = Settings.REDIS_PASSWORD
    cache_instance = CacheConfigurator()
    params = {
        "socket_timeout": 0.5,
    }
    connection_string = f"rediss://:{password}@{host}:{port}"
    cache_instance.setup(connection_string, **params)
    return cache_instance


is_cache_enabled = True
caching_module = initialize_caching(get_settings())
ROUTE_CACHING = caching_module
