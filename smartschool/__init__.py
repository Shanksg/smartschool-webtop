"""SmartSchool (Webtop) homework monitor.

Talks to the SmartSchool (Webtop) web API, reimplemented synchronously with
token rotation, unattended renewal, and message monitoring.
"""

from .client import WebtopClient
from .bio import BioCredentials
from .config import Config, Paths
from .exceptions import (
    ApiError,
    RequestFailed,
    SmartSchoolError,
    TokenExpired,
    TokenMissing,
)
from .models import HomeworkItem, Message, RotationResult, Student, TokenState
from .monitor import Monitor
from .notifiers import Notifier
from .session import TokenStore
from .state import HomeworkState, SeenState

__version__ = "2.0.0"

__all__ = [
    "WebtopClient",
    "BioCredentials",
    "Config",
    "Paths",
    "Monitor",
    "Notifier",
    "TokenStore",
    "HomeworkState",
    "SeenState",
    "Message",
    "HomeworkItem",
    "Student",
    "TokenState",
    "RotationResult",
    "SmartSchoolError",
    "TokenExpired",
    "TokenMissing",
    "RequestFailed",
    "ApiError",
]
