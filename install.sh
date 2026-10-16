#!/usr/bin/env bash
# Instalador rápido para Linux: ./install.sh
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { sudo apt-get update && sudo apt-get install -y python3; }
sudo python3 yt_stream.py install
