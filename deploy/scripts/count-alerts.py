#!/usr/bin/env python3
"""Print the number of firing Prometheus alerts."""
import json
import sys

try:
    data = json.load(sys.stdin)
    alerts = data.get("data", {}).get("alerts", [])
    firing = [a for a in alerts if a.get("state") == "firing"]
    print(f"Firing: {len(firing)}")
except Exception as e:
    print(f"Error: {e}")