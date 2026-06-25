class IapAuthenticationError(Exception):
    pass


class IapMissingHeaderError(IapAuthenticationError):
    pass


class IapInvalidJwtError(IapAuthenticationError):
    pass


class IapMissingEmailInJwtError(IapAuthenticationError):
    pass


class IapJWKRetrievalError(IapAuthenticationError):
    pass
