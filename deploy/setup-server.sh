#!/bin/bash
# =============================================================
# Kalshi order book dashboard: one-time server setup script
# Run this on a fresh AWS EC2 Ubuntu 24.04 instance
#
# EC2 uses 'ubuntu' as the default user (not root).
# Usage:  ssh ubuntu@your-ec2-ip 'bash -s' < setup-server.sh
# =============================================================

set -e  # Stop on any error

# Never stop to ask questions (apt upgrade, needrestart, dpkg-reconfigure)
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

echo "========================================="
echo " Kalshi order book dashboard: server setup (AWS EC2)"
echo "========================================="

# --- 1. System updates ---
echo "[1/7] Updating system packages..."
sudo -E apt update && sudo -E apt upgrade -y

# --- 2. Security: firewall + fail2ban ---
echo "[2/7] Setting up firewall and fail2ban..."
sudo apt install -y ufw fail2ban
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (Caddy redirect)
sudo ufw allow 443/tcp   # HTTPS (Caddy)
sudo ufw --force enable
sudo systemctl enable fail2ban
sudo systemctl start fail2ban

# --- 3. Auto security updates ---
echo "[3/7] Enabling automatic security updates..."
sudo apt install -y unattended-upgrades
echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' | sudo debconf-set-selections
sudo -E dpkg-reconfigure -f noninteractive unattended-upgrades

# --- 4. Install Python ---
echo "[4/7] Installing Python and build tools..."
sudo apt install -y python3 python3-venv python3-pip git

# --- 5. Install Caddy ---
echo "[5/7] Installing Caddy..."
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# --- 6. Set up project directory ---
echo "[6/7] Setting up project directory..."
sudo mkdir -p /opt/tradedash
sudo chown ubuntu:ubuntu /opt/tradedash

# Give ubuntu user limited sudo for the tradedash service (used by GitHub Actions deploy)
echo "ubuntu ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart tradedash, /usr/bin/systemctl stop tradedash, /usr/bin/systemctl start tradedash, /usr/bin/systemctl status tradedash" | sudo tee /etc/sudoers.d/ubuntu-tradedash

# --- 7. Summary ---
echo ""
echo "========================================="
echo " Setup complete!"
echo "========================================="
echo ""
echo " Next steps (do these manually):"
echo ""
echo " 1. Clone your repo:"
echo "    cd /opt/tradedash"
echo "    git clone https://github.com/SouparneyaC/kalshi-orderbook-dashboard.git ."
echo ""
echo " 2. Create virtual environment:"
echo "    python3 -m venv venv"
echo "    venv/bin/pip install -r requirements.txt"
echo ""
echo " 3. Create .env file:"
echo "    cp .env.example .env"
echo "    nano .env  # fill in your Kalshi API keys"
echo ""
echo " 4. Copy your Kalshi private key to the server:"
echo "    (from your Mac) scp /path/to/kalshi_private_key.txt ubuntu@SERVER_IP:/opt/tradedash/"
echo "    chmod 600 /opt/tradedash/kalshi_private_key.txt"
echo ""
echo " 5. Install the systemd service:"
echo "    sudo cp deploy/tradedash.service /etc/systemd/system/"
echo "    sudo systemctl daemon-reload"
echo "    sudo systemctl enable tradedash"
echo "    sudo systemctl start tradedash"
echo ""
echo " 6. Set up Caddy (replace domain in Caddyfile first):"
echo "    sudo cp deploy/Caddyfile /etc/caddy/Caddyfile"
echo "    sudo systemctl restart caddy"
echo ""
echo " 7. Check it's running:"
echo "    sudo systemctl status tradedash"
echo "    journalctl -u tradedash -f  (live logs)"
echo ""
echo "========================================="
