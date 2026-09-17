class AffiliateDailyPending(RuntimeError):
    """A scheduled daily post remains pending without being considered failed."""


class AffiliateRunStopped(RuntimeError):
    """The user requested a stop at a safe workflow boundary."""
