# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

ENV R_CLIENT_ID="NnlgLHzMwE2MSpC4fsMQUQ"
ENV R_CLIENT_SECRET="MxeC9Yst237ss00TPAD69KpHm5LCng"
ENV R_USER_AGENT="seeky/1.0 (by /u/Objective_Crazy_5434)"
ENV R_USERNAME="Objective_Crazy_5434"
ENV R_PASSWORD="Luka170917340186!"  
ENV MONGODB_URI="mongodb+srv://luka:1CYzf6RXmvjrtGzr@bitpulse.mu20dsa.mongodb.net"


# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Default command
CMD ["python", "scrape_reddit_posts.py"] 