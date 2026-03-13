#!/bin/zsh
# shellcheck disable=SC2034 # Variables used in other contexts
# shellcheck disable=SC2086 # Parsing variables

# OpenClaw Scout Runner Script

# --- Constants and Configuration ---
readonly AGENT_NAME="OpenClaw Scout"
readonly TASK_NAME="Morning Brief Generation and Email"
readonly BASE_DIR="$HOME/.openclaw/scout"
readonly LOG_DIR="$HOME/.openclaw/logs"
readonly SCOUT_LOG="$LOG_DIR/scout_oc.log"
readonly SCOUT_ERR_LOG="$LOG_DIR/scout_oc.err.log"
readonly AGENT_LABEL="scout_oc" # For launchd and other OpenClaw internal references
readonly RECIPIENT_EMAIL="chip.ernst@gmail.com"
readonly RECIPIENT_NAME="Chip Ernst"
readonly TIMEZONE="America/New_York"
readonly DAY_FOLDER=$(date +%Y-%m-%d) # YYYY-MM-DD format for folder name

# Trilium Note ID (as per user instruction)
readonly TRILIUM_NOTE_ID="bZVTkWU9Uo9l"

# --- Helper functions for communication contract ---

# ACK: Acknowledge task start
# Usage: ACK <timestamp> <what_you_re_doing>
function ack_start {
  local timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")
  echo "ACK $timestamp $1"
}

# HB: Heartbeat during execution
# Usage: HB <timestamp> <status_message>
function heartbeat {
  local timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")
  echo "HB $timestamp $1"
}

# DONE: Report successful completion
# Usage: DONE <timestamp> files=<path> new_use_cases=<count> top_finding=<finding>
function done_task {
  local timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")
  local files="$1"
  local new_use_cases="$2"
  local top_finding="$3"
  echo "DONE $timestamp files=$files new_use_cases=$new_use_cases top_finding=$top_finding"
}

# FAIL: Report task failure
# Usage: FAIL <timestamp> <step_failed>
function fail_task {
  local timestamp=$(date +"%Y-%m-%d %H:%M:%S %Z")
  local step="$1"
  echo "FAIL $timestamp $step" >&2 # Output to stderr for error logs
  exit 1 # Exit with non-zero status
}

# --- Main script logic ---

# Redirect stdout and stderr to log files
exec > >(tee -a "$SCOUT_LOG") 2> >(tee -a "$SCOUT_ERR_LOG" >&2)

# 1. Acknowledge task start
ack_start "$TASK_NAME"

# 2. Create directory for today's run
echo "Creating working directory: $BASE_DIR/$DAY_FOLDER"
mkdir -p "$BASE_DIR/$DAY_FOLDER" || fail_task "mkdir $BASE_DIR/$DAY_FOLDER"

# 3. Generate Morning Brief markdown
# Placeholder: This is where the logic to fetch data and generate the brief would go.
# For now, we'll create a dummy brief.
echo "Generating Morning Brief..."
BRIEF_CONTENT="## Morning Brief - $(date +'%Y-%m-%d')

**Today's Focus:**

*   Reviewing OpenClaw Scout performance.
*   Processing user requests.

**System Status:**

*   Gateway: [See openclaw gateway probe output]
*   WhatsApp: Linked
*   Agent Model: active

**(Placeholder for more detailed findings, data points, etc.)**"

echo "$BRIEF_CONTENT" > "$BASE_DIR/$DAY_FOLDER/morning_brief.md" || fail_task "write morning brief"
echo "Morning Brief generated."

# 4. Update running use-case table (CSV + MD)
# Placeholder: Logic to update tables would be here.
echo "Updating use-case table..."
# Example: Append to a CSV
echo "$(date +'%Y-%m-%d'),Scout Task Run,,Morning Brief Generated" >> "$BASE_DIR/$DAY_FOLDER/use_cases.csv"
# Example: Append to a Markdown table
echo "| $(date +'%Y-%m-%d') | Scout Task Run | Morning Brief Generated |" >> "$BASE_DIR/$DAY_FOLDER/use_cases.md"
echo "Use-case table updated."

# 5. Send Morning Brief by email to Chip
echo "Sending Morning Brief to $RECIPIENT_NAME ($RECIPIENT_EMAIL)..."
# Using sendmail command assuming it's available and configured.
# For actual sending, we'd need to ensure the brief content is correctly formatted.
# For a dry run, we can just print the email command.
EMAIL_SUBJECT="Morning Brief - $(date +'%Y-%m-%d')"
EMAIL_BODY_FILE="$BASE_DIR/$DAY_FOLDER/morning_brief.md"

# Check if sendmail command is available
if command -v sendmail > /dev/null; then
  # Constructing the email header and body
  {
    echo "To: $RECIPIENT_NAME <$RECIPIENT_EMAIL>"
    echo "From: OpenClaw Scout <hendrik.homarus@gmail.com>" # Assuming this sender address
    echo "Subject: $EMAIL_SUBJECT"
    echo "Content-Type: text/plain; charset=UTF-8"
    echo ""
    cat "$EMAIL_BODY_FILE"
  } | sendmail -t || fail_task "sendmail"
  echo "Email sent successfully."
else
  echo "sendmail command not found. Cannot send email."
  # Decide how to handle this: fail or proceed with a warning.
  # For now, we'll proceed but note it.
  heartbeat "sendmail command not found, skipping email. Manual intervention needed."
fi

# 6. Emit DONE marker
# This is simplified, as actual counts/findings would come from the brief generation.
# Placeholder values used here.
NUM_USE_CASES=1 # Dummy value
TOP_FINDING="Placeholder finding"
done_task "$BASE_DIR/$DAY_FOLDER" "$NUM_USE_CASES" "$TOP_FINDING"

exit 0
