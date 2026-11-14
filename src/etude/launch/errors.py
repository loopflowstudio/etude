from etude.errors import LauncherError as LauncherError


class LaunchError(LauncherError):
    """Identifies launch evidence, including when a controller reply is uncertain."""

    def __init__(self, launch: str, runs: tuple[str, ...], message: str) -> None:
        self.launch, self.runs = launch, runs
        super().__init__(
            f"launch {launch} for {', '.join(runs)}: {message}; inspect etude logs --run {runs[0]}"
        )


class ProviderUnavailable(LauncherError):
    """No usable provider answer. This never means the requested thing is absent."""


class SchemaAdvanced(LauncherError):
    """A breaking upgrade fenced this process; program identifies its successor."""

    def __init__(self, program: str | None) -> None:
        super().__init__("etude was upgraded while this ran; run it again")
        self.program = program
