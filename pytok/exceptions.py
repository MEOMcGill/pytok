class TikTokException(Exception):
    """Generic exception that all other TikTok errors are children of."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class CaptchaException(TikTokException):
    """TikTok is showing captcha"""


class NotFoundException(TikTokException):
    """TikTok indicated that this object does not exist."""


class EmptyResponseException(TikTokException):
    """TikTok sent back an empty response."""


class ResponseValidationException(EmptyResponseException):
    """TikTok returned a well-formed (statusCode==0) response, but it failed the
    caller's content validation (e.g. the expected fields are missing).

    A subclass of EmptyResponseException so it is treated as a request-level
    failure (the session is still good — bot-detection / a degraded API response,
    not a dead tab): make_request keeps the session and retries, then the caller
    falls back to frontend scraping, rather than invalidating the session and
    tearing down the browser."""


class SoundRemovedException(TikTokException):
    """This TikTok sound has no id from being removed by TikTok."""


class InvalidJSONException(TikTokException):
    """TikTok returned invalid JSON."""


class NotAvailableException(TikTokException):
    """The requested object is not available in this region."""

class NoContentException(TikTokException):
    """TikTok returned no content"""

class TimeoutException(TikTokException):
    """Timed out trying to get content from TikTok"""

class ApiFailedException(TikTokException):
    """TikTok API is failing"""

class FewerVideosThanExpectedException(TikTokException):
    """TikTok is returning fewer videos for this user than their metadata led us to expect"""

class AccountPrivateException(TikTokException):
    """This TikTok account is private and cannot be scraped"""

class LoginException(TikTokException):
    """TikTok requires login to view this content"""