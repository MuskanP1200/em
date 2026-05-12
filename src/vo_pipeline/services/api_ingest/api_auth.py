import logging
import requests
import xmltodict

logger = logging.getLogger(__name__)

AUTH_XML = """<AuthenticateUserRQ xmlns="http://erac.com/appsec/enhanced/rsi/webservice/auth">
    <Request xmlns="">
        <CallerIdentity/>
        <CallingProcess/>
        <CallingApplicationName>vrservices</CallingApplicationName>
        <CallingApplicationVersion>1</CallingApplicationVersion>
        <CallingInterfaceVersion/>
        <CallingHostOrWeblogicInstance>tomcat9090:9090</CallingHostOrWeblogicInstance>
        <RequestId>1</RequestId>
        <CacheId/>
    </Request>
    <Locale xmlns="">
        <ISOCountryCode>US</ISOCountryCode>
        <ISOLanguageCode>en</ISOLanguageCode>
    </Locale>
    <AppStaticId xmlns="">759935158</AppStaticId>
    <LogonId xmlns="">{username}</LogonId>
    <Password xmlns="">{password}</Password>
    <OrigEvent xmlns=""/>
</AuthenticateUserRQ>"""


def get_token(username: str, password: str, auth_url: str) -> str:
    """
    Authenticates with AppSec and returns a session token.

    Args:
        username: Service account username.
        password: Service account password.
        auth_url: Auth endpoint URL (passed from main config).

    Returns:
        Auth token string.

    Raises:
        RuntimeError: If authentication fails or token not found.
    """
    logger.info("Requesting AppSec token...")

    response = requests.post(
        auth_url,
        headers={"Content-Type": "application/xml", "Accept": "application/xml"},
        data=AUTH_XML.format(username=username, password=password),
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Auth failed [{response.status_code}]: {response.text}")

    token = (
        xmltodict.parse(response.text).get("auth:AuthenticateUserRS", {}).get("Token")
    )

    if not token:
        raise RuntimeError(f"Token not found in response: {response.text}")

    return token


# if __name__ == "__main__":
#     token = get_token(username=, password=)

#     print(f"token: {token}")
