"""Exceptions raised by the gicisky_296_bwry package."""


class GiciskyError(Exception):
    """Base exception for the driver."""


class GiciskyConnectionError(GiciskyError):
    """Could not connect to the tag."""


class GiciskyProtocolError(GiciskyError):
    """The tag returned an unexpected or malformed protocol response."""


class GiciskyTransferError(GiciskyError):
    """Image transfer to the tag failed."""
