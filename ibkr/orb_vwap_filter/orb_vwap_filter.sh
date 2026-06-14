#!/bin/bash

# ORB + VWAP Filter Bot Runner
# This script runs every minute via cron to execute the bot

# Change to the script directory
cd "$(dirname "$0")"

# Set up Python environment if needed
# Uncomment and modify if you're using a virtual environment:
# source /path/to/venv/bin/activate

# Log file
LOG_FILE="orb_vwap_filter.log"

# Timestamp for logging
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Run the bot and capture output
echo "[$TIMESTAMP] Running ORB VWAP bot..." >> "$LOG_FILE"
python3 orb_vwap_filter.py >> "$LOG_FILE" 2>&1

# Check exit status
if [ $? -eq 0 ]; then
    echo "[$TIMESTAMP] Bot completed successfully" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] Bot failed with error code $?" >> "$LOG_FILE"
fi

# Keep log file size reasonable (keep last 1000 lines)
tail -n 1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"

# Optional: Clean up old state files (older than 7 days)
find . -name "bot_state.json.*.bak" -mtime +7 -delete 2>/dev/null

exit 0
