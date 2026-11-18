"""Exceptions raised by mcsc.

Messages are plain English; applications that need localized messages
should catch these types and translate them.
"""


class MCSCError(Exception):
    """Base class for every error raised by mcsc."""


class ServerNotFoundError(MCSCError, KeyError):
    """No server with the requested id is registered."""


class ServerStateError(MCSCError):
    """The operation is not allowed in the server's current state."""


class ServerRunningError(ServerStateError):
    """The operation requires the server to be stopped."""


class ServerNotRunningError(ServerStateError):
    """The operation requires the server to be running."""


class EulaNotAcceptedError(ServerStateError):
    """The server cannot start until the Minecraft EULA is accepted."""


class JavaNotFoundError(MCSCError, FileNotFoundError):
    """The configured Java executable could not be found."""


class VersionNotFoundError(MCSCError, LookupError):
    """The requested Minecraft version is not available for the server type."""


class UnsupportedVersionError(MCSCError):
    """The version is known, but mcsc cannot install or run it."""


class DownloadError(MCSCError):
    """A file could not be downloaded."""


class InstallError(MCSCError):
    """Server installation failed."""
