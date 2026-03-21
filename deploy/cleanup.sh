#!/bin/bash
# =============================================================
# SNAP-AI — Docker Disk Cleanup
# =============================================================
# Frees disk space by removing unused Docker resources.
# Safe to run while the app is running — only removes unused items.
#
# Usage: bash deploy/cleanup.sh
# =============================================================
set -euo pipefail

echo "SNAP-AI Docker Cleanup"
echo "======================"

echo ""
echo "Before:"
docker system df 2>/dev/null || true

echo ""
echo "Removing unused containers, networks, images..."
docker system prune -af --filter "until=72h" 2>/dev/null || docker system prune -af

echo ""
echo "Removing dangling volumes..."
docker volume prune -f 2>/dev/null || true

echo ""
echo "After:"
docker system df 2>/dev/null || true

echo ""
echo "✓ Cleanup complete"
