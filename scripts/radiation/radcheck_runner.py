import subprocess
import sys
import os

SCORE_SCRIPT_PATH = "/Users/AGENT/.openclaw/workspace/openclaw-ops/scripts/radiation/radcheck_scoring_v2.py"
HISTORY_LOG_PATH = os.path.expanduser("~/.openclaw/watchdog/radcheck_history.ndjson")
OPS_EVENTS_LOG_PATH = os.path.expanduser("~/.openclaw/watchdog/ops_events.log")

def run_radcheck_scoring():
    """Executes the RadCheck scoring script and captures its output."""
    try:
        # Ensure the scoring script is executable
        os.chmod(SCORE_SCRIPT_PATH, 0o755)

        # Construct the command to run the script using sys.executable
        command = [sys.executable, SCORE_SCRIPT_PATH]

        # Execute the script and capture stdout and stderr
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        print("--- RadCheck Scoring Output ---")
        print(result.stdout)
        if result.stderr:
            print("--- RadCheck Scoring Errors ---")
            print(result.stderr)

    except FileNotFoundError:
        print(f"Error: Script not found at {SCORE_SCRIPT_PATH}")
    except subprocess.CalledProcessError as e:
        print(f"Error executing script: {e}")
        print(f"Stderr: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def examine_logs():
    """Reads and prints the contents of the RadCheck log files."""
    print("\n--- RadCheck History Log ---")
    try:
        with open(HISTORY_LOG_PATH, "r") as f:
            print(f.read())
    except FileNotFoundError:
        print(f"Log file not found: {HISTORY_LOG_PATH}")
    except Exception as e:
        print(f"Error reading log file {HISTORY_LOG_PATH}: {e}")

    print("\n--- Operations Events Log ---")
    try:
        with open(OPS_EVENTS_LOG_PATH, "r") as f:
            print(f.read())
    except FileNotFoundError:
        print(f"Log file not found: {OPS_EVENTS_LOG_PATH}")
    except Exception as e:
        print(f"Error reading log file {OPS_EVENTS_LOG_PATH}: {e}")

if __name__ == "__main__":
    run_radcheck_scoring()
    examine_logs()
