#!/bin/sh
set -eu
mkdir -p /run/voice
# A fresh log prevents an earlier boot's URL being published on restart.
: > /run/voice/startup.log
python /srv/save_endpoint.py &
exec cloudflared "$@" --logfile /run/voice/startup.log
