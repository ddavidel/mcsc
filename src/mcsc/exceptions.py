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


class ServerBusyError(ServerStateError):
    """Another operation (backup, restore, export, delete) is running on the server."""


class BackupError(MCSCError):
    """A backup, restore, export or import failed."""


class InsufficientDiskSpaceError(BackupError, OSError):
    """There is not enough free disk space for the operation."""


class InvalidArchiveError(BackupError):
    """The archive is not a valid mcsc backup/export (corrupted, no manifest, unsafe entries)."""


class UnsupportedArchiveVersionError(InvalidArchiveError):
    """The archive was created by a newer version of mcsc."""


class IncompatibleBackupError(BackupError):
    """The backup belongs to a different server type than the server it is restored into."""


class InvalidServerFolderError(MCSCError):
    """The folder doesn't look like a Minecraft server that can be imported."""
