# Reddit Scraper API - Management Dashboard

The easiest way to manage multiple Reddit scrapers through a web interface!

## What You Get

🖥️ **Web Dashboard**: Simple interface to start/stop scrapers  
🔄 **Multi-Subreddit Management**: Run multiple scrapers simultaneously  
🐳 **Docker Containers**: Each scraper runs in its own isolated Docker container  
🔐 **Unique Credentials**: Each scraper uses separate Reddit API credentials to avoid rate limits  
📊 **Real-time Statistics**: View scraping progress and data collected  
⚙️ **Easy Configuration**: Preset configurations for different subreddit types  
🚀 **RESTful API**: Full API for automation and integration

## Prerequisites

- **Docker**: Required for running scrapers in containers
- **MongoDB Atlas**: Free cloud database for storing scraped data
- **Multiple Reddit Apps**: One per subreddit you want to scrape

## Quick Start (3 Steps!)

### Step 1: Set Up Environment

Create a `.env` file with your MongoDB connection (Reddit credentials are provided per scraper):

```bash
# MongoDB Atlas (free database) - shared across all scrapers
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/reddit_data
```

**Important**: Each scraper runs in its own Docker container with unique Reddit API credentials to avoid rate limit conflicts.

### Step 2: Build Docker Image & Start API

```bash
# Build the scraper Docker image first
docker build -f Dockerfile -t reddit-scraper .

# Start the API server
docker-compose -f docker-compose.api.yml up -d

# Or run directly (requires Docker)
pip install -r requirements.txt
python api.py
```

### Step 3: Open Web Dashboard

Go to: **http://localhost:8000**

That's it! You now have a web interface that launches Docker containers for each Reddit scraper.

## How It Works

### Container Architecture

- **API Server**: Runs the management dashboard
- **Scraper Containers**: Each subreddit gets its own Docker container
- **Isolated Credentials**: Each container has unique Reddit API credentials
- **Resource Management**: Docker handles memory, CPU limits per scraper
- **Easy Scaling**: Add/remove scrapers without affecting others

### Container Naming

Containers are automatically named: `reddit-scraper-{subreddit}`

Examples:

- `reddit-scraper-wallstreetbets`
- `reddit-scraper-stocks`
- `reddit-scraper-investing`

## Setting Up Multiple Reddit Apps

Since each scraper needs unique credentials, you'll need to create multiple Reddit applications:

### Create Reddit Apps

1. Go to https://www.reddit.com/prefs/apps
2. Click "Create App" or "Create Another App"
3. Choose "script" type
4. Fill in name and description
5. **Repeat for each scraper you want to run**

### Why Multiple Apps?

- **Rate Limit Isolation**: Each app gets its own rate limit quota
- **Container Isolation**: Credentials are isolated per Docker container
- **Better Performance**: No competition between scrapers
- **Compliance**: Follows Reddit's best practices
- **Reliability**: One scraper's issues won't affect others

## Using the Web Dashboard

### Start a New Scraper

1. **Enter subreddit name** (without r/)
2. **Choose preset**:
   - **High Activity**: For wallstreetbets, stocks (2000 posts, 3 min intervals)
   - **Medium Activity**: For investing, crypto (1000 posts, 5 min intervals)
   - **Low Activity**: For pennystocks, niche (500 posts, 10 min intervals)
3. **Expand "Reddit API Credentials" section**
4. **Enter unique credentials** for this scraper:
   - Client ID and Secret from your Reddit app
   - Reddit username and password
   - Descriptive user agent
5. **Click "Start Scraper"**

### Monitor Your Scrapers

- **Green boxes**: Running containers
- **Gray boxes**: Stopped containers
- **Red boxes**: Containers with errors
- **Container ID**: Shows Docker container identifier
- **Reddit User**: Shows which Reddit account each container uses

### View Logs & Statistics

- **📊 Stats**: View scraping statistics for the subreddit
- **📋 Logs**: View real-time logs from the Docker container

## API Endpoints

### Container Management

```bash
# List all scrapers and their container status
GET http://localhost:8000/scrapers

# Start a new scraper container
POST http://localhost:8000/scrapers/start
{
  "subreddit": "wallstreetbets",
  "posts_limit": 2000,
  "interval": 180,
  "comment_batch": 30,
  "credentials": {
    "client_id": "your_app_client_id",
    "client_secret": "your_app_client_secret",
    "username": "your_reddit_username",
    "password": "your_reddit_password",
    "user_agent": "WallStreetBetsScraper/1.0 by YourUsername"
  },
  "mongodb_uri": "mongodb+srv://..." // optional
}

# Stop a scraper container
POST http://localhost:8000/scrapers/wallstreetbets/stop

# Get container logs
GET http://localhost:8000/scrapers/wallstreetbets/logs?lines=100

# Get statistics
GET http://localhost:8000/scrapers/wallstreetbets/stats

# Get container status
GET http://localhost:8000/scrapers/wallstreetbets/status

# Health check (includes Docker status)
GET http://localhost:8000/health
```

### Docker Commands (Alternative)

You can also manage containers directly:

```bash
# List running scraper containers
docker ps --filter "name=reddit-scraper-"

# View logs from a specific container
docker logs reddit-scraper-wallstreetbets

# Stop a container manually
docker stop reddit-scraper-wallstreetbets

# View container resource usage
docker stats reddit-scraper-wallstreetbets
```

## Example Use Cases

### Financial Data Collection (Multiple Containers)

```bash
# Container 1: wallstreetbets
curl -X POST http://localhost:8000/scrapers/start \
  -H "Content-Type: application/json" \
  -d '{
    "subreddit": "wallstreetbets",
    "posts_limit": 2000,
    "interval": 180,
    "comment_batch": 30,
    "credentials": {
      "client_id": "app1_client_id",
      "client_secret": "app1_secret",
      "username": "reddit_user1",
      "password": "password1",
      "user_agent": "WSBScraper/1.0 by User1"
    }
  }'

# Container 2: stocks
curl -X POST http://localhost:8000/scrapers/start \
  -H "Content-Type: application/json" \
  -d '{
    "subreddit": "stocks",
    "posts_limit": 1500,
    "interval": 240,
    "comment_batch": 25,
    "credentials": {
      "client_id": "app2_client_id",
      "client_secret": "app2_secret",
      "username": "reddit_user2",
      "password": "password2",
      "user_agent": "StocksScraper/1.0 by User2"
    }
  }'
```

### Container Monitoring Script

```python
import requests
import time

def monitor_containers():
    health = requests.get("http://localhost:8000/health").json()
    print(f"Docker Available: {health['docker_available']}")
    print(f"Running Containers: {health['running_containers']}")

    scrapers = requests.get("http://localhost:8000/scrapers").json()
    for subreddit, info in scrapers.items():
        print(f"r/{subreddit}: {info['status']} (container: {info['container_name']})")

        if info['status'] == 'error':
            # Get logs for debugging
            logs = requests.get(f"http://localhost:8000/scrapers/{subreddit}/logs").json()
            print(f"  Recent logs: {logs['logs'][-200:]}")  # Last 200 chars

# Monitor every minute
while True:
    monitor_containers()
    time.sleep(60)
```

## Docker Usage

### Build Required Image

```bash
# Build the scraper image (required)
docker build -f Dockerfile -t reddit-scraper .

# Start API server
docker-compose -f docker-compose.api.yml up -d

# View API logs
docker-compose -f docker-compose.api.yml logs -f

# Stop API server
docker-compose -f docker-compose.api.yml down
```

### Container Resource Management

```bash
# Set memory limit for containers (optional)
docker run --memory="512m" --name reddit-scraper-test reddit-scraper

# View resource usage of all scraper containers
docker stats $(docker ps --filter "name=reddit-scraper-" --format "{{.Names}}")

# Clean up stopped containers
docker container prune
```

## Troubleshooting

### Docker Issues

```bash
# Check if Docker is running
docker --version
docker ps

# Check if reddit-scraper image exists
docker images | grep reddit-scraper

# Build image if missing
docker build -f Dockerfile -t reddit-scraper .
```

### API Won't Start

```bash
# Check if port 8000 is already in use
lsof -i :8000

# Check API logs
docker-compose -f docker-compose.api.yml logs reddit-scraper-api
```

### Container Won't Start

1. **Check Docker**: Ensure Docker daemon is running
2. **Check Image**: Verify `reddit-scraper` image exists
3. **Check Credentials**: Verify Reddit API credentials are correct
4. **Check Logs**: Use the logs endpoint to see container output

```bash
# Check if image exists
docker images reddit-scraper

# Build image if missing
docker build -f Dockerfile -t reddit-scraper .

# Check container logs via API
curl http://localhost:8000/scrapers/wallstreetbets/logs
```

### Reddit API Issues

- **Invalid Credentials**: Double-check client ID, secret, username, password
- **Rate Limits**: Each container has separate quota - check Reddit app dashboard
- **User Agent**: Must be unique and descriptive per Reddit guidelines
- **App Type**: Ensure Reddit app is set to "script" type

### Container Management

```bash
# Force stop all scraper containers
docker stop $(docker ps --filter "name=reddit-scraper-" -q)

# Remove all scraper containers
docker rm $(docker ps --filter "name=reddit-scraper-" -a -q)

# View detailed container information
docker inspect reddit-scraper-wallstreetbets
```

## Security Features

### Container Isolation

- **Process Isolation**: Each scraper runs in its own container
- **Credential Isolation**: Environment variables are isolated per container
- **Resource Limits**: Docker prevents containers from affecting each other
- **Network Isolation**: Containers can't access each other's data

### Safe Practices

```bash
# Use descriptive, unique user agents per container
"WSBScraper/1.0 by YourUsername"
"StockAnalysis/1.0 by ResearchTeam"
"CryptoTracker/1.0 by TradingBot"

# Create dedicated Reddit accounts for scraping (optional but recommended)
# Use strong, unique passwords
# Enable 2FA on main Reddit account (scraping apps use app passwords)
```

This container-based approach provides excellent isolation, scalability, and resource management for running multiple Reddit scrapers!
