import yaml
import logging
import logging.config
from pathlib import Path
from typing import Union

try:
    from settings import AppEnvironment, get_settings
except ImportError:
    from api.settings import AppEnvironment, get_settings

settings = get_settings()


class AppNameFilter(logging.Filter):
    def __init__(self, app_name: str):
        super().__init__()
        self.app_name = app_name

    def filter(self, record):
        record.app_name_field = self.app_name
        return True


DEFAULT_LOGGING_CONFIG = Path(__file__).parent / "config.yaml"


def configure_logging(
    settings: settings,
    config_file: Union[str, Path] = DEFAULT_LOGGING_CONFIG,
    patch_root_log_level: bool = True,
):
    with open(config_file, "r") as lf:
        config_from_file = yaml.safe_load(lf)

    # Ensure log directory exists
    if "file" in config_from_file.get("handlers", {}):
        log_file = config_from_file["handlers"]["file"].get("filename")
        if log_file:
            log_dir = Path(log_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig(config_from_file)

    app_filter = AppNameFilter(settings.APP_NAME)
    for handler in logging.root.handlers:
        handler.addFilter(app_filter)

    root_logger = logging.getLogger()

    if patch_root_log_level:
        level = settings.log_level.upper()

        if settings.APP_ENVIRONMENT == AppEnvironment.PRODUCTION:
            if level in ["DEBUG", "TRACE"]:
                level = "INFO"

        root_logger.setLevel(level)
