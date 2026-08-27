"""Exception types.

Every failure path in conclave raises. Nothing returns ``None`` to signal an
error, because the single nastiest failure mode this library guards against --
an auth failure that reports ``subtype: "success"`` and exits 0 -- is already
hard enough to notice without adding sentinel returns on top.
"""

from __future__ import annotations


class ConclaveError(RuntimeError):
    """Base class for every error this library raises."""


class CLIError(ConclaveError):
    """The ``claude`` subprocess failed.

    Covers: binary missing from PATH, non-zero exit, timeout, unparseable
    envelope, and -- most importantly -- an envelope flagged ``is_error``,
    which is how the CLI reports auth failure while exiting 0.
    """


class AuthError(ConclaveError):
    """Not logged in, or logged into an account you did not intend to bill.

    Raised by :func:`conclave.auth.assert_account` and by preflight. Kept
    distinct from :class:`CLIError` so a caller can retry transport failures
    without retrying a billing mistake.
    """


class BudgetError(ConclaveError):
    """A configured spend ceiling was reached before the call was made."""


class ExpertNotFound(ConclaveError):
    """No expert by that name is registered."""


class SessionError(ConclaveError):
    """A session could not be started, resumed, or spoken to."""
