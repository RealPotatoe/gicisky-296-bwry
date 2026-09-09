"""Exceptions raised by the gicisky_296_bwry package."""


class GiciskyError(Exception):
    """Base exception for the driver."""


class GiciskyConnectionError(GiciskyError):
    """BLE connection, subscription, I/O, or disconnection failed."""


class GiciskyProtocolError(GiciskyError):
    """A protocol exchange timed out or returned an invalid response."""


class GiciskyTransferError(GiciskyError):
    """The tag rejected a transfer, or its time/retry budget was exhausted."""
