#!/bin/bash
set -e

echo "Starting the  Deployment for VisitPro..."

# 1. Pull latest code
echo "Pulling latest code from github"
git pull origin main

# 2. Build and restart containers
echo "Building and starting the  containers..."
docker compose up -d --build

# 3. Run database migrations
echo "Running the database migrations..."
docker compose exec -it fastapi alembic upgrade head

# 4. Clean up unused images
echo "Cleaning up all the old images..."
docker image prune -f

echo "Congratulations! Deployment Successful!"
