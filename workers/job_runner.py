"""Background job runner scaffold."""


class JobRunner:
    """Coordinates execution of asynchronous or queued supervisor work.

    TODO:
    - Select a real background execution backend.
    - Define job retry, timeout, and dead-letter behavior.
    - Integrate with persistent task state updates.
    """

    def run_pending(self) -> None:
        """Run queued jobs once the backend is implemented."""
        raise NotImplementedError("Background job execution is not implemented yet.")

