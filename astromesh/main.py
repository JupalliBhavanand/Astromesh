"""Launch the AstroMesh web dashboard and telemetry service."""

from pathlib import Path

import uvicorn


def main() -> None:
    # This entry point is the local development launcher. Watch backend source so
    # API changes are loaded automatically; the dashboard build watcher refreshes
    # already-open tabs when frontend assets change.
    source_dir = Path(__file__).resolve().parent
    uvicorn.run(
        "astromesh.server.api:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[str(source_dir)],
    )


if __name__ == "__main__":
    main()
