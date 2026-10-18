class LauncherError(Exception):
    """The request cannot be carried out as asked."""


class LaunchError(LauncherError):
    """Identifies launch evidence, including when a controller reply is uncertain."""

    def __init__(self, launch: str, runs: tuple[str, ...], message: str) -> None:
        self.launch, self.runs = launch, runs
        super().__init__(
            f"launch {launch} for {', '.join(runs)}: {message}; inspect etude logs --run {runs[0]}"
        )
