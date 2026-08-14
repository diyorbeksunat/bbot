# Legalix Mandat Bot — Render Web Service

This version keeps Telegram long-polling and the mandat monitoring worker, while also opening an HTTP health server on 0.0.0.0:$PORT so Render Web Service stays healthy.

## Render
- Type: Web Service
- Runtime: Docker
- Dockerfile Path: ./Dockerfile
- Docker Command: leave empty
- Pre-Deploy Command: leave empty
- Health Check Path: /health
- Persistent Disk mount: /var/data

## Notes
The bot token is currently embedded in the Python file for the test setup requested. Rotate it after testing because it has been exposed during development.
