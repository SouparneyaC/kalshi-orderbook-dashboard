# Deployment

This is how we run the service: one Ubuntu 24.04 server, systemd to keep the process alive, and Caddy in front for a password. The same setup is in `deploy/`.

## What the setup script does

`deploy/setup-server.sh` runs once on a fresh machine (`ssh ubuntu@your-ip 'bash -s' < deploy/setup-server.sh`). It stops on the first error and takes seven steps:

| Step | What it does |
|---|---|
| 1 | Updates the system packages |
| 2 | Installs the `ufw` firewall and `fail2ban`. The firewall denies incoming traffic except ports 22, 80 and 443 |
| 3 | Turns on automatic security updates |
| 4 | Installs Python 3, venv, pip and git |
| 5 | Installs Caddy from its package repository |
| 6 | Creates `/opt/tradedash`, and lets the `ubuntu` user restart, stop, start and check only the `tradedash` service with sudo |
| 7 | Prints the manual steps that follow |

## The manual steps

1. Clone the repository into `/opt/tradedash`, create a virtual environment and install `requirements-lock.txt` (or `requirements.txt` for the newest compatible versions).
2. Copy `.env.example` to `.env` and set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
3. Copy your Kalshi private key to the server and run `chmod 600` on it.
4. Install `deploy/tradedash.service`, then enable and start it.
5. Put your hashed password in `deploy/Caddyfile`, copy it to `/etc/caddy/Caddyfile` and restart Caddy.
6. Check it with `sudo systemctl status tradedash` and `journalctl -u tradedash -f`.

## The three pieces

| Piece | File | What it does |
|---|---|---|
| systemd unit | `deploy/tradedash.service` | Runs uvicorn on 127.0.0.1:8000 as the `ubuntu` user, reads `.env`, restarts the process 5 seconds after it exits, and sends output to the journal |
| Caddy | `deploy/Caddyfile` | Asks for a password and proxies to the app. With a domain name it gets an HTTPS certificate on its own. Without one it serves plain HTTP on the IP |
| Firewall | `ufw`, set by the script | Only SSH, HTTP and HTTPS reach the machine |

The app listens on 127.0.0.1 only, so everything reaches it through Caddy.

## SSH

The script does not change the SSH configuration. On EC2 you log in with the key pair you chose when you launched the instance, and that is how our instance accepts SSH: by key only. We also limited port 22 to one IP address in the AWS security group, which the script cannot do. On another provider, check that password login is off (`PasswordAuthentication no` in `/etc/ssh/sshd_config`) and keep a second terminal open while you test the change.

## Instance size

Our instance is an AWS EC2 t4g.small (2 vCPUs, 2 GiB of memory) in us-east-2 (Ohio), the same AWS region as Kalshi's API host. A t4g instance is burstable: it earns CPU credits and is slowed to a baseline once they run out. See [benchmarks.md](benchmarks.md) for CPU use over 48 hours, and the README for the day our credits ran out with trade storage on.

## Docker

`Dockerfile` and `compose.yaml` are a way to try the service without installing Python. They are not what we measured. The container runs the same code under uvicorn, with trade storage off and the measurement history in `./data`.
