import json
import logging
import logging.config

from datetime import datetime, timezone
from pathlib import Path
from typing import Union

import yaml

from api.settings import AppEnvironment, get_settings


settings = get_settings()

DEFAULT_LOGGING_CONFIG = (
    Path(__file__).parent / "config.yaml"
)


class ContextFilter(logging.Filter):
    """
    Injects enterprise/common fields
    into every log record.
    """

    def filter(self, record):

        record.AppName = getattr(
            settings,
            "APP_NAME",
            "unknown-app"
        )

        record.AppVersion = getattr(
            settings,
            "APP_VERSION",
            "0.0.0"
        )

        record.Env = str(
            getattr(
                settings,
                "APP_ENVIRONMENT",
                "dev"
            )
        ).lower()

        return True


class JsonFormatter(logging.Formatter):

    def format(self, record):

        log_payload = {

            # REQUIRED FIRST FIELD
            "Timestamp": datetime.now(
                timezone.utc
            ).isoformat(),

            # Standard fields
            "Level": record.levelname,

            "AppName": getattr(
                record,
                "AppName",
                None
            ),

            "AppVersion": getattr(
                record,
                "AppVersion",
                None
            ),

            "Env": getattr(
                record,
                "Env",
                None
            ),

            "EventName": getattr(
                record,
                "EventName",
                record.msg
            ),

            "EventType": getattr(
                record,
                "EventType",
                "custom"
            ),

            "GenericPath": getattr(
                record,
                "GenericPath",
                None
            ),

            "SpanId": getattr(
                record,
                "SpanId",
                None
            ),

            "TraceId": getattr(
                record,
                "TraceId",
                None
            ),

            "WorkflowId": getattr(
                record,
                "WorkflowId",
                None
            ),

            "Url": getattr(
                record,
                "Url",
                None
            ),

            "Logger": record.name,

            "Message": record.getMessage(),

            # Custom payload
            "Data": getattr(
                record,
                "Data",
                {}
            ),
        }

        # Add exception stacktrace if present
        if record.exc_info:

            log_payload["Exception"] = (
                self.formatException(
                    record.exc_info
                )
            )

        return json.dumps(
            log_payload,
            default=str
        )


def configure_logging(
    settings,
    config_file: Union[str, Path] = DEFAULT_LOGGING_CONFIG,
    patch_root_log_level: bool = True,
):

    with open(config_file, "r") as lf:

        config_from_file = yaml.safe_load(lf)

    logging.config.dictConfig(
        config_from_file
    )

    context_filter = ContextFilter()

    # Attach filter to all handlers
    for handler in logging.root.handlers:

        handler.addFilter(context_filter)

    root_logger = logging.getLogger()

    # Dynamically patch root level
    if patch_root_log_level:

        level = settings.log_level.upper()

        # Prevent DEBUG logging in PROD
        if (
            settings.APP_ENVIRONMENT
            == AppEnvironment.PRODUCTION
        ):

            if level in ["DEBUG", "TRACE"]:

                level = "INFO"

        root_logger.setLevel(level)