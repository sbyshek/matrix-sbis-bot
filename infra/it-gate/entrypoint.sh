#!/bin/sh
# Портал-API с автоперезапуском
while true; do
    uvicorn main:app --host 127.0.0.1 --port 8001 >> /var/log/portal-api.log 2>&1
    echo "portal-api died, restarting..." >> /var/log/portal-api.log
    sleep 2
done &

# nginx — основной процесс
nginx -g "daemon off;"