#!/bin/bash

# Test script for ORB + VWAP Filter Bot setup

echo "========================================="
echo "ORB + VWAP Filter Bot Setup Test"
echo "========================================="
echo ""

# Check if files exist
echo "1. Checking files..."
files=("orb_vwap_filter.py" "orb_vwap_filter.sh" "README.md" "QUICK_REFERENCE.md")
for file in "${files[@]}"; do
    if [ -f "$file" ]; then
        echo "   ✓ $file exists"
    else
        echo "   ✗ $file missing"
    fi
done
echo ""

# Check if shell script is executable
echo "2. Checking permissions..."
if [ -x "orb_vwap_filter.sh" ]; then
    echo "   ✓ orb_vwap_filter.sh is executable"
else
    echo "   ✗ orb_vwap_filter.sh is not executable"
    echo "   Run: chmod +x orb_vwap_filter.sh"
fi
echo ""

# Check Python dependencies
echo "3. Checking Python dependencies..."
python3 -c "import ib_insync" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "   ✓ ib_insync installed"
else
    echo "   ✗ ib_insync not installed"
    echo "   Run: pip install ib_insync"
fi

python3 -c "import numpy" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "   ✓ numpy installed"
else
    echo "   ✗ numpy not installed"
    echo "   Run: pip install numpy"
fi
echo ""

# Test IBKR connection (optional)
echo "4. Testing IBKR connection (optional)..."
echo "   Attempting to connect to IBKR at 172.31.9.221:4001..."

python3 << 'EOF'
from ib_insync import IB
import sys

try:
    ib = IB()
    ib.connect('172.31.9.221', 4001, clientId=99, timeout=10)
    print("   ✓ Successfully connected to IBKR")
    ib.disconnect()
    sys.exit(0)
except Exception as e:
    print(f"   ✗ Failed to connect to IBKR: {e}")
    print("   Make sure TWS/Gateway is running and API is enabled")
    sys.exit(1)
EOF

echo ""

# Check cron setup
echo "5. Checking cron configuration..."
crontab -l 2>/dev/null | grep -q "orb_vwap_filter.sh"
if [ $? -eq 0 ]; then
    echo "   ✓ Cron job found"
    echo "   Current cron entries for orb_vwap_filter:"
    crontab -l 2>/dev/null | grep "orb_vwap_filter.sh"
else
    echo "   ✗ Cron job not configured"
    echo "   To add cron job, run: crontab -e"
    echo "   Add line: * * * * * $(pwd)/orb_vwap_filter.sh"
fi
echo ""

echo "========================================="
echo "Test Complete"
echo "========================================="
echo ""
echo "To run the bot manually:"
echo "  python3 orb_vwap_filter.py"
echo ""
echo "To view the dashboard:"
echo "  open bot.html"
echo ""
echo "To monitor logs:"
echo "  tail -f orb_vwap_filter.log"
echo ""
