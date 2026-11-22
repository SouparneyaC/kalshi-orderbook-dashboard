# Security policy

## Reporting a problem

Report a vulnerability through GitHub's private form: open the Security tab of this repository and choose "Report a vulnerability". Do not open a public issue for it.

Please include what you found, the version or commit, and the steps that show it. This is a one-person project. I will acknowledge a report within 14 days and tell you what I plan to do about it, but I cannot promise a fix date.

## Supported versions

Only the latest release and the `main` branch receive fixes.

## What the service holds

The service signs requests to Kalshi with your API key. Three things follow:

- Keep the private key file readable by your user only (`chmod 600`), and never commit it. `.gitignore` excludes `*.pem`, `*.key` and `.env`.
- The app has no login of its own. It listens on 127.0.0.1, and `deploy/Caddyfile` shows how to put a password in front of it. Do not expose port 8000 to the internet directly.
- If a key leaks, revoke it in your Kalshi account settings first, then create a new one.

The service has no order path. It subscribes to Kalshi's feed and reads order book snapshots and market details. It does not place or cancel orders.
