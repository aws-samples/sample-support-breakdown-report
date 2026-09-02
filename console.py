"""Interactive console for the Support Breakdown Report.

A thin REPL wrapper around ``script.py``. The ``run`` command forwards any
CLI-style options straight to ``script.main()`` in the same process, so the
console stays in sync with the script without duplicating its argument list.
"""

import os
import shlex
# subprocess is used only with a fixed literal command and no shell (see clear_screen).
import subprocess  # nosec B404

import script

# ASCII-art banner shown on startup and after 'clear'.
BANNER = r"""
╭───────────────────────────────────────────╮
│         Support Breakdown Report          │
│      ___                         ___      │
│     /\__\         _____         /\  \     │
│    /:/ _/_       /::\  \       /::\  \    │
│   /:/ /\  \     /:/\:\  \     /:/\:\__\   │
│  /:/ /::\  \   /:/ /::\__\   /:/ /:/  /   │
│ /:/_/:/\:\__\ /:/_/:/\:|__| /:/_/:/__/___ │
│ \:\/:/ /:/  / \:\/:/ /:/  / \:\/:::::/  / │
│  \::/ /:/  /   \::/_/:/  /   \::/~~/~~~~  │
│   \/_/:/  /     \:\/:/  /     \:\~~\      │
│     /:/  /       \::/  /       \:\__\     │
│     \/__/         \/__/         \/__/     │
│ ───────────────────────────────────────── │
│            Interactive Console            │
╰───────────────────────────────────────────╯
"""

HELP_TEXT = """Available commands:
  help              Show this help message
  quit / exit       Exit the console
  clear             Clear the screen
  run [options]     Generate the Support Breakdown Report

The 'run' command accepts the same options as script.py, for example:
  run                                 Last complete month -> support-breakdown-report-<timestamp>.csv
  run --lookback 3                    Last 3 complete months
  run --billing-month 2026-07         A specific billing month
  run --output report.csv             Write to a custom path ('-' for stdout)
  run --profile my-profile            Use a specific AWS named profile
  run --help                          Show all available options
"""

def clear_screen():
    """Clear the terminal screen (cross-platform).

    Uses subprocess.run with an argument list (no shell) so the command is not
    interpreted by a shell. The command is a fixed literal with no user input,
    but avoiding the shell removes the injection surface entirely.
    """
    # The command is passed as a static literal argument list with the shell
    # disabled, so there is no user input and no command-injection surface.
    if os.name == "nt":
        subprocess.run(["cls"], shell=False, check=False)  # nosec B603
    else:
        subprocess.run(["clear"], shell=False, check=False)  # nosec B603


def run_report(arg_string):
    """Run the Support Breakdown Report in-process, forwarding CLI-style arguments.

    ``arg_string`` is everything typed after 'run'. It is split like a shell
    command line and handed to ``script.main()`` as argv.
    """
    try:
        argv = shlex.split(arg_string)
    except ValueError as exc:
        # e.g. an unbalanced quote in the entered arguments.
        print(f"Could not parse arguments: {exc}")
        return

    print("Running the Support Breakdown Report...")
    try:
        script.main(argv)
    except SystemExit as exc:
        # argparse raises SystemExit for '--help' or invalid options; catch it
        # so the console keeps running instead of exiting with the script.
        if exc.code not in (0, None):
            print(f"Report exited with status {exc.code}.")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        print(f"Report failed: {exc}")


def main():
    """Run the interactive read-eval-print loop."""
    clear_screen()
    print(BANNER)
    print("Welcome to the Support Breakdown Report console.")
    print("Type 'help' for a list of commands.")
    print()

    while True:
        try:
            line = input("support-breakdown> ").strip()
        except EOFError:
            # Ctrl-D: exit the loop cleanly.
            print()
            break
        except KeyboardInterrupt:
            # Ctrl-C: exit the loop cleanly.
            print()
            break

        if not line:
            continue

        # Split the input into the command word and the remaining argument text.
        command, _, rest = line.partition(" ")
        command = command.lower()
        rest = rest.strip()

        if command in ("quit", "exit"):
            clear_screen()
            break
        elif command == "help":
            print(HELP_TEXT)
        elif command == "clear":
            clear_screen()
            print(BANNER)
        elif command == "run":
            run_report(rest)
        else:
            print(f"Unknown command: {command}")


if __name__ == "__main__":
    main()