"""Exception hierarchy for the SmartSchool/Webtop client.

TokenExpired is distinct from a generic request failure, because it is the
only error the monitor can act on (renew, or prompt for a fresh token).
"""


class SmartSchoolError(Exception):
    """Base class for every error raised by this package."""


class TokenExpired(SmartSchoolError):
    """The webToken is no longer accepted (HTTP 401, or status=false on a token check).

    Recovered automatically when a bioLogin credential is configured: the
    monitor mints a fresh token via user/loginByBio (Monitor.renew_via_bio()).
    Only without that credential does it need a human - SmartSchool gates login
    behind a reCAPTCHA checkbox - and it then notifies for a manual paste.
    """


class TokenMissing(SmartSchoolError):
    """No token is available at all - nothing in the cache or token.txt."""


class RequestFailed(SmartSchoolError):
    """A request reached the server but did not return a usable payload."""


class ApiError(RequestFailed):
    """The API returned HTTP 200 with status=false.

    Carries the server's own error fields so callers can tell
    'view is blocked' apart from a genuine failure.
    """

    def __init__(self, message, *, error_description=None, error_id=None, payload=None):
        super().__init__(message)
        self.error_description = error_description
        self.error_id = error_id
        self.payload = payload
