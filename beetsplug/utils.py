class _PlexQueryException(Exception):
    """Base class for all PlexQuery exceptions."""

    pass


class NotFound(_PlexQueryException):
    """Request media item or device is not found."""

    pass


class ValueError(_PlexQueryException):
    """Value error."""

    pass


class UnhandledError(_PlexQueryException):
    """Unhandled error."""

    pass
