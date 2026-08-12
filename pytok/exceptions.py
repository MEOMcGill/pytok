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

class NoVideoException(NotAvailableException):
    """This post has no video to download — it is a photo/slideshow post.

    A property of the post, not of the session or the account, so a caller collecting media
    should skip it and carry on rather than counting it as a download failure. Photo-heavy
    profiles post these in long runs, which would otherwise look like a broken session.

    Subclasses NotAvailableException so callers that only catch that keep working. A caller
    which maps NotAvailableException to "this account is unavailable, skip the handle" has to
    catch this one *first*: one photo post says nothing about the account."""

class NoContentException(TikTokException):
    """TikTok returned no content"""

class TimeoutException(TikTokException):
    """Timed out trying to get content from TikTok"""

class CDPTimeoutException(TikTokException):
    """A CDP command was sent to the browser and never answered.

    Means the page has stopped servicing the DevTools protocol, so every later command on
    that connection would hang too — the session is dead and has to be rebuilt.

    Deliberately a plain TikTokException: it is in neither the accounts pool's
    DATA_LEVEL_EXCEPTIONS (which propagate with no retry — this is not the target's fault)
    nor ROTATE_EXCEPTIONS (which cooldown the account for minutes — the account is fine, it
    is the browser that is broken). That drops it into Worker.execute_task's generic handler,
    which closes the session and rebuilds in place on the same account: the fast recovery
    this actually wants."""

class ApiFailedException(TikTokException):
    """TikTok API is failing"""

class NoTemplateException(ApiFailedException):
    """No browser param template has been captured yet for this API endpoint.

    The param cache is lazily filled: the first request for an endpoint type
    must go through the frontend scraping route, which captures the webapp's
    own request params off the wire. A subclass of ApiFailedException so every
    existing API→scraping fallback handles it transparently."""

class ListingTruncatedException(ApiFailedException):
    """A listing walk stopped partway through a profile without TikTok saying it had ended.

    Distinct from the bare "no videos at all" case: a profile's first page comes from the page
    HTML, which a bot-flagged session still receives, so a blocked walk yields a page or two
    and then dies looking like a complete profile.

    A subclass of ApiFailedException so existing API→scraping fallbacks and the pool's
    ROTATE_EXCEPTIONS keep handling it unchanged, but called out separately in
    Worker.execute_task's cooldown policy: it says nothing bad about the account, only that
    this session could not paginate."""

class FewerVideosThanExpectedException(TikTokException):
    """TikTok is returning fewer videos for this user than their metadata led us to expect"""

class AccountPrivateException(TikTokException):
    """This TikTok account is private and cannot be scraped"""

class LoginException(TikTokException):
    """TikTok requires login to view this content"""