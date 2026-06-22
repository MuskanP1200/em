import logging
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Union

import jwt
from jwt import PyJWKClient

from .errors import IapInvalidJwtError, IapJWKRetrievalError
from .iap_jwt_settings import IapJwtSettings

logger = logging.getLogger(__name__)


class IapAuthenticationBackend:
    """
    Backend to decode JWTs based on typical application settings

    See the model ``IapJwtSettings`` for information on how to set these values.
    """

    def __init__(self, settings: IapJwtSettings) -> None:
        self.settings = settings
        self._jwks_client = PyJWKClient(settings.AZURE_JWK_URL, cache_keys=True)

    def decode(self, token_from_header: Union[bytes, str]) -> Mapping[str, Any]:
        """
        Reads and validates a JWT token to settings.

        The backend does not understand what a middleware is: its purpose
        is to decode a validate JWTs.

        Args:
            token_from_header: The JWT payload to decode.

        Returns:
            The decoded JWT token data as a ``Mapping`` (Dictionary)

        Raises:
            IapInvalidJwtError: If the token is not valid.
            IapJWKRetrievalError: if unable to retrieve the JWK signign key.
        """
        if token_from_header is None:
            raise ValueError("A value is expected to be provided for the token.")

        try:
            logger.debug(f"Retrieved JWK @ {self.settings.AZURE_JWK_URL}")
            jwks_client = PyJWKClient(self.settings.AZURE_JWK_URL)
            signing_key = jwks_client.get_signing_key_from_jwt(token_from_header)
        except Exception as e:
            logger.exception(e)
            raise IapJWKRetrievalError("Unable to retrieve JWKS signing key") from e

        try:
            data = self._decode_azure_jwt(
                token_from_header,
                signing_key=signing_key.key,
                expected_audience=self.settings.jwt_expected_audience,
                issuer=self.settings.AZURE_JWT_EXPECTED_ISS,
                algorithms=self.settings.AZURE_JWT_EXPECTED_ALGORITHMS,
                timedelta=self.settings.AZURE_JWT_LEEWAY,
            )

        except Exception as e:
            self.log_jwt_error(token_from_header, e)
            raise IapInvalidJwtError from e
        return data

    def log_jwt_error(self, token, e: Exception):
        # Adapted from jwt module_load method
        def extracted_aud_unsafe(tok):
            from jwt.utils import base64url_decode
            import json

            if isinstance(tok, (str, bytes)):
                tok = tok.encode("utf-8")
            signing_input, _ = tok.rsplit(b".", 1)
            _, payload_segment = signing_input.split(b".", 1)
            payload = base64url_decode(payload_segment)
            return json.loads(payload.decode("utf-8")).get("aud")

        try:
            logger.debug("Gathering audience from token")
            detected_audience = extracted_aud_unsafe(token)
            logger.error(
                "Invalid audience. expected: {}, received: {}".format(
                    self.settings.jwt_expected_audience, detected_audience
                )
            )
            logger.exception(e)
        except Exception:
            logger.warning("Error while trying to log audience")

    @staticmethod
    def _decode_azure_jwt(
        token: Union[bytes, str],
        signing_key: str,
        expected_audience: str,
        issuer: str,
        algorithms: List[str],
        timedelta: Optional[timedelta] = None,
    ) -> Dict[str, Any]:
        """
        Decode JWT based on IAP configs in application settings and returns the payload.

        Requires that `exp` and `iat` claims are present and valid.

        Args:
            token: A signed JWT token as a string or as bytes
            signing_key: The key to use to check the signature
            expected_audience: The claim `aud` that be the token
            issuer: The claim `iss` that should be the token
            algorithms: A list of algorithms that are valid for decoding
            timedelta: The leeway for the `exp` claims

        Returns:
            A dictionnary containing the validated payload (excluding the header and the signature).
        """
        return jwt.decode(
            token,
            signing_key,
            algorithms=algorithms,
            issuer=issuer,
            audience=expected_audience,
            options={
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["exp", "iat"],
            },
            leeway=timedelta,
        )
