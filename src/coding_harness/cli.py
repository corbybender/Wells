"""Interactive REPL CLI for the Wells coding harness."""

import os
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.markdown import Markdown
from langchain_core.callbacks import BaseCallbackHandler

from coding_harness import config, settings
from coding_harness.graph import build_graph
from coding_harness.tokens import LEDGER
from coding_harness.main import _print_final_summary, _print_info, _reload_module_config

console = Console()

style = Style.from_dict({
    'prompt': '#00aa00 bold',
})

class StreamingCallback(BaseCallbackHandler):
    """Streams LLM tokens to the console."""
    def on_llm_new_token(self, token: str, **kwargs) -> None:
        if config.STREAM_OUTPUT:
            sys.stdout.write(token)
            sys.stdout.flush()

    def on_llm_end(self, response, **kwargs) -> None:
        if config.STREAM_OUTPUT:
            sys.stdout.write("\n")
            sys.stdout.flush()

def print_welcome() -> None:
    console.print("\n[bold blue]Wells Coding Harness[/bold blue]")
    console.print(f"Model: {config.model_name_for_task('coding')}")
    console.print(f"Workspace: {config.WORKSPACE_ROOT}  (safety: {config.HARNESS_SAFETY})")
    console.print("Type your goal, or [bold]/help[/bold] for commands.\n")

def handle_slash_command(command: str) -> bool:
    """Handles slash commands. Returns False if REPL should exit, True otherwise."""
    cmd = command.strip().lower()
    if cmd in ("/quit", "/exit"):
        return False
    elif cmd == "/help":
        console.print("\n[bold]Available Commands:[/bold]")
        console.print("  [cyan]/quit[/cyan]   - Exit the REPL")
        console.print("  [cyan]/config[/cyan] - Open the interactive settings menu")
        console.print("  [cyan]/info[/cyan]   - Print effective configuration")
        console.print("  [cyan]/plan[/cyan]   - Toggle plan mode")
        console.print()
    elif cmd == "/config":
        settings.interactive_menu(Path(".env"))
        _reload_module_config()
    elif cmd == "/info":
        _reload_module_config()
        _print_info()
    elif cmd == "/plan":
        current = os.environ.get("PLAN_MODE", "0")
        new_val = "0" if current not in ("0", "false", "no", "") else "1"
        os.environ["PLAN_MODE"] = new_val
        _reload_module_config()
        console.print(f"\nPlan mode is now: [bold]{'ON' if config.PLAN_MODE else 'OFF'}[/bold]\n")
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
    return True

def run_repl() -> None:
    if not _ensure_model_configured():
        return
        
    print_welcome()
    session = PromptSession()
    
    app = build_graph()
    
    # Maintain conversational state across loops
    agent_state = {
        "iteration": 0,
        "max_iterations": config.MAX_ITERATIONS,
        "workspace_root": config.WORKSPACE_ROOT,
        "safety": config.HARNESS_SAFETY,
        "plan_mode": config.PLAN_MODE,
        "messages": [],
        "executor_messages": [],
    }

    callbacks = [StreamingCallback()]

    while True:
        try:
            text = session.prompt(HTML('<prompt>Wells&gt;</prompt> '), style=style).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
            
        if not text:
            continue
            
        if text.startswith("/"):
            if not handle_slash_command(text):
                break
            continue
            
        # Update goal
        agent_state["goal"] = text
        agent_state["iteration"] = 0
        LEDGER.reset()
        
        console.print(f"\n[bold cyan]Executing:[/bold cyan] {text}\n")
        
        try:
            # We use stream to get node updates
            for update in app.stream(agent_state, config={"callbacks": callbacks}, stream_mode="updates"):
                for node_name, node_state in update.items():
                    console.print(f"\n[bold magenta]>> {node_name.upper()} <<[/bold magenta]")
                    if not config.STREAM_OUTPUT:
                        console.print(f"Completed step: {node_name}")
                    
                    # Merge node_state back into our persistent agent_state
                    for k, v in node_state.items():
                        agent_state[k] = v
                        
            _print_final_summary(agent_state)
            console.print("\n" + LEDGER.format_report())
        except Exception as e:
            console.print(f"\n[bold red]Error during execution:[/bold red] {e}")

def _ensure_model_configured() -> bool:
    from coding_harness.main import _ensure_model_configured as check
    return check()

if __name__ == "__main__":
    run_repl()
