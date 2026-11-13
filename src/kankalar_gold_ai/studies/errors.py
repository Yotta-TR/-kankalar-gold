"""Typed failures for study configuration and registry operations."""


class StudyError(Exception):
    """Base class for study-interface and registry errors."""


class StudyConfigError(StudyError, ValueError):
    """A study configuration or configuration-dependent value is invalid."""


class InvalidStudyError(StudyError, ValueError):
    """A study's metadata or required interface is invalid."""


class DuplicateStudyError(StudyError):
    """A study ID and version are already registered."""


class UnknownStudyError(StudyError, LookupError):
    """A requested study ID or explicit version is not registered."""


class StudyRequestError(StudyError, ValueError):
    """A study run request, supplied data or data-failure report is invalid."""


class StudyExecutionError(StudyError):
    """The runner could not safely construct a StudyResult."""


class StudyPersistenceError(StudyError):
    """Study result persistence or cache lookup failed or returned invalid data."""
