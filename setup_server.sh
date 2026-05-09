#!/bin/bash
set -e

echo "Setting up VisitPro Server Environment..."

# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install Docker
if ! command -v docker &> /dev/null; then
    echo "Installing the Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
fi

# Install Docker Compose (if not already part of docker)
if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose..."
    sudo apt-get install docker-compose-v2 -y
fi

# Install Git and Certbot
echo "Installing Git, Certbot, and Nginx (for SSL challenge)..."
sudo apt-get install -y git certbot python3-certbot-nginx

# Create directory structure
mkdir -p /home/fawas/visitpro
mkdir -p /var/www/certbot

echo "Server setup complete!"
echo "Next steps:"
echo "1. Clone the repository into /home/fawas/visitpro"
echo "2. Create the .env file in /home/fawas/visitpro/backend/.env"
echo "3. Run your first deploy: ./deploy.sh"
echo "4. After deployment, run: sudo certbot certonly --webroot -w /var/www/certbot -d visitpro.de -d www.visitpro.de"
echo "5. Uncomment the SSL block in nginx/nginx.conf and run ./deploy.sh again."
