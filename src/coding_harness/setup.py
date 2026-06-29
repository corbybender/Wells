"""Auto-setup on first run: install Rust, build indexer, prompt for workspace, auto-index."""

import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()


def _ensure_rust_installed() -> bool:
    """Check if Rust is installed; if not, install it via rustup.

    Returns True if Rust is available (already or after install).
    """
    # Check if rustc exists
    try:
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        pass

    # Rust not found; try to install via rustup
    console.print("[cyan]Installing Rust toolchain (needed for indexer)...[/cyan]")

    try:
        # Windows
        if sys.platform == "win32":
            console.print("[cyan]Downloading rustup installer...[/cyan]")
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                    "Invoke-WebRequest -Uri 'https://win.rustup.rs' -OutFile 'rustup-init.exe'; "
                    ".\\rustup-init.exe -y",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                console.print(
                    f"[yellow]Rust install failed. Install manually: https://rustup.rs[/yellow]"
                )
                return False
        else:
            # macOS/Linux
            console.print("[cyan]Downloading rustup...[/cyan]")
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                console.print(
                    f"[yellow]Rust install failed. Install manually: https://rustup.rs[/yellow]"
                )
                return False

        # Verify installation
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            console.print("[green]✓ Rust installed successfully[/green]")
            return True
        else:
            console.print("[yellow]Rust installed but verification failed[/yellow]")
            return False

    except Exception as e:
        console.print(f"[yellow]Could not install Rust: {e}[/yellow]")
        console.print("[yellow]Install manually from: https://rustup.rs[/yellow]")
        return False


def _ensure_indexer_built() -> bool:
    """Build wells-index if not already installed.

    Returns True if indexer is available (either already installed or successfully built).
    """
    try:
        import wells_index  # noqa: F401
        console.print("[green]✓ Indexer already available[/green]")
        return True
    except ImportError:
        pass

    # Try to build from local source
    wells_root = Path(__file__).parent.parent.parent
    indexer_dir = wells_root / "wells-index"

    if not indexer_dir.exists():
        console.print("[red]ERROR: wells-index source not found at {indexer_dir}[/red]")
        return False

    console.print("[cyan]Building indexer (first time only, this may take a minute)...[/cyan]")

    try:
        # Install maturin first
        console.print("[dim]Installing maturin...[/dim]")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "maturin"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            console.print(f"[red]Failed to install maturin: {result.stderr[:200]}[/red]")
            return False

        # Build with maturin (show output for debugging)
        console.print("[dim]Compiling Rust extension...[/dim]")
        result = subprocess.run(
            ["maturin", "develop", "-q"],
            cwd=str(indexer_dir),
            env={**os.environ, "UV_LINK_MODE": "copy"},
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            console.print(f"[red]Indexer build failed with exit code {result.returncode}[/red]")
            console.print("[yellow]Trying alternative build method...[/yellow]")
            # Try pip install -e as fallback
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", str(indexer_dir)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                console.print(f"[red]Build failed: {result.stderr[:500]}[/red]")
                return False

        # Verify it worked
        try:
            import wells_index  # noqa: F401
            console.print("[green]✓ Indexer built and installed successfully[/green]")
            return True
        except ImportError:
            console.print("[red]Build succeeded but indexer still not importable[/red]")
            return False

    except subprocess.TimeoutExpired:
        console.print("[red]Indexer build timed out (>10min)[/red]")
        return False
    except Exception as e:
        console.print(f"[red]Error building indexer: {e}[/red]")
        return False


def _prompt_for_workspace() -> str | None:
    """Ask user for workspace path on first run."""
    from pathlib import Path

    console.print("\n[bold cyan]First run setup[/bold cyan]")
    console.print("Enter the path to your project (or press Enter to skip indexing for now):")
    console.print("Example: Q:\\myproject  or  /home/me/myproject\n")

    try:
        path_input = input("> ").strip()
        if not path_input:
            return None

        path = Path(path_input).expanduser().resolve()
        if not path.exists():
            console.print(f"[red]Path does not exist: {path}[/red]")
            return None
        if not path.is_dir():
            console.print(f"[red]Not a directory: {path}[/red]")
            return None

        return str(path)
    except KeyboardInterrupt:
        return None


def _auto_index_workspace(workspace: str) -> bool:
    """Auto-index the workspace on first run."""
    from coding_harness import index_tools
    from coding_harness.tools import ToolContext

    console.print(f"\n[cyan]Indexing {workspace}...[/cyan]")
    try:
        ctx = ToolContext(workspace=workspace)
        result = index_tools.index_workspace(ctx)
        if result.ok:
            console.print(f"[green]{result.output}[/green]")
            return True
        else:
            console.print(f"[yellow]Indexing incomplete: {result.error or result.output}[/yellow]")
            return False
    except Exception as e:
        console.print(f"[yellow]Could not index workspace: {e}[/yellow]")
        return False


def first_run_setup() -> None:
    """Run setup on first use: install Rust, build indexer, ask for workspace, auto-index."""
    from coding_harness import config

    # Check if already set up (workspace defined, indexer available)
    if config.WORKSPACE_ROOT != os.getcwd():
        # Workspace already configured
        return

    # Ensure Rust is installed
    rust_ok = _ensure_rust_installed()
    if not rust_ok:
        console.print("[yellow]Rust toolchain required. Skipping indexer setup.[/yellow]")
        return

    # Try to build indexer
    indexer_ok = _ensure_indexer_built()
    if not indexer_ok:
        console.print("[yellow]Indexer not available. Using grep for code search.[/yellow]")
        return

    # Prompt for workspace
    workspace = _prompt_for_workspace()
    if not workspace:
        return

    # Save to .env
    try:
        from coding_harness import settings
        settings.update_env_file(Path(".env"), {"WORKSPACE_ROOT": workspace})
        os.environ["WORKSPACE_ROOT"] = workspace
    except Exception as e:
        console.print(f"[yellow]Could not save workspace to .env: {e}[/yellow]")

    # Auto-index
    _auto_index_workspace(workspace)
    console.print()
