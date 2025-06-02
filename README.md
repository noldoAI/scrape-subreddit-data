# Reddit Scraper API

A comprehensive Reddit scraping system with a web-based management API that orchestrates multiple containerized scrapers. Each scraper can use unique Reddit API credentials and target different subreddits simultaneously, with automatic monitoring, restart capabilities, and persistent storage.

## 🚀 Quick Start

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

### 2. Use the Web Dashboard

1. **Open** `http://localhost:8000` in your browser
2. **Configure** your Reddit API credentials
3. **Select** target subreddit and scraping parameters
4. **Start** the scraper and monitor via dashboard
5. **View** real-time statistics and logs

## 🏗️ Architecture Overview

The system consists of three main components:

### **🎯 Management API (Primary Interface)**

- **Web Dashboard**: User-friendly interface for managing scrapers
- **REST API**: Programmatic control of all scraping operations
- **Docker Management**: Automatic container orchestration
- **Database Storage**: Persistent scraper configurations and credentials
- **Monitoring**: Health checks, auto-restart, and failure detection

### **📦 Containerized Scrapers**

- **Isolated Execution**: Each subreddit runs in its own Docker container
- **Unique Credentials**: Separate Reddit API credentials per scraper
- **Unified Scraping**: Posts, comments, and metadata in one process
- **Smart Scheduling**: Intelligent comment update prioritization

### **🗄️ MongoDB Database**

- **Scraped Data**: Posts, comments, subreddit metadata
- **Configuration**: Scraper settings and encrypted credentials
- **Monitoring**: Performance metrics and status tracking

## 📊 How Scraping Works

Each scraper runs a continuous 3-phase cycle:

### **Phase 1: Posts Scraping** (Every 5 minutes)

```
1. Fetch hot posts from target subreddit using Reddit API
2. Extract post metadata (title, score, author, timestamps, etc.)
3. Update existing posts with new scores/comment counts
4. Store new posts that entered the hot list
5. Preserve comment tracking status for existing posts
```

### **Phase 2: Smart Comment Updates** (Continuous)

```
Priority Queue System:
1. HIGHEST: Posts never scraped for comments (initial scrape)
2. HIGH: Recent posts (<24h) - update every 6 hours
3. MEDIUM: Older posts - update every 24 hours
4. Deduplication: Only collect new comments, skip existing ones
5. Hierarchical: Preserve parent-child comment relationships
```

### **Phase 3: Subreddit Metadata** (Every 24 hours)

```
1. Scrape subreddit information (subscribers, rules, settings)
2. Track community growth and changes over time
3. Store visual elements and descriptions
4. Update only when 24+ hours have passed
```

## 🎮 Using the Web Dashboard

### **Starting a New Scraper**

1. **Subreddit Configuration**:

   - Target subreddit name (without r/)
   - Preset configurations (High/Medium/Low activity)
   - Custom parameters (posts limit, interval, comment batch size)

2. **Reddit API Credentials** (Required):

   ```
   Client ID:     Your Reddit app client ID
   Client Secret: Your Reddit app client secret
   Username:      Your Reddit username
   Password:      Your Reddit password
   User Agent:    RedditScraper/1.0 by YourUsername
   ```

3. **Advanced Options**:
   - Auto-restart on failure (enabled by default)
   - Custom MongoDB URI (optional)

### **Managing Scrapers**

- **📊 View Statistics**: Posts, comments, completion rates
- **📋 Check Logs**: Real-time container logs
- **🔄 Restart**: Manual restart failed scrapers
- **⏹️ Stop/Start**: Control individual scrapers
- **🗑️ Delete**: Remove scraper and configuration
- **⚙️ Auto-restart**: Toggle automatic failure recovery

### **Monitoring Dashboard**

```
System Health:
✅ Database: Connected
✅ Docker: Available
📊 Total Scrapers: 3
🏃 Running: 2
❌ Failed: 1
```

## 🔧 REST API Endpoints

### **Scraper Management**

```bash
# Start new scraper
POST /scrapers/start
{
  "subreddit": "wallstreetbets",
  "posts_limit": 2000,
  "interval": 180,
  "comment_batch": 30,
  "credentials": { ... },
  "auto_restart": true
}

# List all scrapers
GET /scrapers

# Get scraper status
GET /scrapers/{subreddit}/status

# Stop scraper
POST /scrapers/{subreddit}/stop

# Restart scraper
POST /scrapers/{subreddit}/restart

# Delete scraper
DELETE /scrapers/{subreddit}

# Get scraper statistics
GET /scrapers/{subreddit}/stats

# Get scraper logs
GET /scrapers/{subreddit}/logs?lines=100
```

### **System Monitoring**

```bash
# System health check
GET /health

# Configuration presets
GET /presets

# Restart all failed scrapers
POST /scrapers/restart-all-failed

# Status summary
GET /scrapers/status-summary
```

## ⚙️ Configuration Presets

### **High Activity Subreddits** (wallstreetbets, stocks)

```json
{
  "posts_limit": 2000,
  "interval": 180,
  "comment_batch": 30
}
```

### **Medium Activity Subreddits** (investing, cryptocurrency)

```json
{
  "posts_limit": 1000,
  "interval": 300,
  "comment_batch": 20
}
```

### **Low Activity Subreddits** (pennystocks, niche topics)

```json
{
  "posts_limit": 500,
  "interval": 600,
  "comment_batch": 10
}
```

## 🗃️ Database Schema

### **Posts Collection** (`reddit_posts`)

```json
{
  "post_id": "abc123",
  "title": "Post title",
  "url": "https://example.com",
  "reddit_url": "https://reddit.com/r/sub/comments/abc123/title/",
  "score": 1500,
  "num_comments": 250,
  "created_utc": 1642694400,
  "created_datetime": "2022-01-20T12:00:00",
  "author": "username",
  "subreddit": "wallstreetbets",
  "selftext": "Post content...",
  "comments_scraped": true,
  "initial_comments_scraped": true,
  "last_comment_fetch_time": "2022-01-20T12:30:00",
  "scraped_at": "2022-01-20T12:00:00"
}
```

### **Comments Collection** (`reddit_comments`)

```json
{
  "comment_id": "def456",
  "post_id": "abc123",
  "parent_id": null,
  "parent_type": "post",
  "author": "commenter",
  "body": "Comment text...",
  "score": 50,
  "depth": 0,
  "created_utc": 1642694500,
  "created_datetime": "2022-01-20T12:01:40",
  "subreddit": "wallstreetbets",
  "scraped_at": "2022-01-20T12:01:40"
}
```

### **Subreddit Metadata Collection** (`subreddit_metadata`)

```json
{
  "subreddit_name": "wallstreetbets",
  "display_name": "wallstreetbets",
  "title": "WallStreetBets",
  "public_description": "Like 4chan found a Bloomberg Terminal",
  "subscribers": 15000000,
  "active_user_count": 45000,
  "over_18": false,
  "lang": "en",
  "created_utc": 1234567890,
  "allow_images": true,
  "allow_videos": true,
  "scraped_at": "2022-01-20T12:00:00",
  "last_updated": "2022-01-20T12:00:00"
}
```

### **Scrapers Collection** (`reddit_scrapers`)

```json
{
  "subreddit": "wallstreetbets",
  "status": "running",
  "container_id": "container123",
  "container_name": "reddit-scraper-wallstreetbets",
  "config": {
    "posts_limit": 2000,
    "interval": 180,
    "comment_batch": 30
  },
  "credentials": {
    "client_id": "encrypted_value",
    "client_secret": "encrypted_value",
    "username": "reddit_user",
    "password": "encrypted_value",
    "user_agent": "RedditScraper/1.0"
  },
  "auto_restart": true,
  "created_at": "2022-01-20T12:00:00",
  "last_updated": "2022-01-20T12:00:00",
  "restart_count": 0
}
```

## 🔐 Security Features

### **Credential Encryption**

- All sensitive credentials encrypted before database storage
- Automatic encryption key generation and management
- Credentials never stored in plain text or logs
- Masked values in API responses and dashboard

### **Container Isolation**

- Each scraper runs in isolated Docker container
- No credential sharing between scrapers
- Individual failure containment
- Resource isolation and management

## 📈 Monitoring & Alerting

### **Automatic Health Checks**

```
✅ Container Status: Monitor Docker containers every 30 seconds
✅ Database Connectivity: Verify MongoDB connection
✅ API Responsiveness: Health check endpoint
✅ Failure Detection: Automatic restart on container failure
✅ Rate Limit Monitoring: Track Reddit API usage
```

### **Real-time Logs**

```bash
# View live logs
2024-01-20 15:30:45 - reddit-scraper - INFO - 🔗 Authenticated as: user123
2024-01-20 15:30:45 - reddit-scraper - INFO - 🎯 Target subreddit: r/wallstreetbets
2024-01-20 15:30:46 - rate-limits - INFO - Rate limit - Remaining: 598, Used: 2
2024-01-20 15:30:47 - reddit-scraper - INFO - Successfully scraped 1000 posts
2024-01-20 15:30:50 - reddit-scraper - INFO - Found 25 new comments
```

## 🛠️ Installation & Setup

### **Prerequisites**

- Docker & Docker Compose
- MongoDB Atlas account (or local MongoDB)
- Reddit API credentials

### **Environment Setup**

1. **Clone Repository**:

```bash
git clone <repository-url>
cd scrape-subreddit-data
```

2. **Create Environment File** (`.env`):

```bash
# MongoDB Connection
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/dbname

# Reddit API Credentials (for API server - optional)
R_CLIENT_ID=your_client_id
R_CLIENT_SECRET=your_client_secret
R_USERNAME=your_username
R_PASSWORD=your_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
```

3. **Get Reddit API Credentials**:

   - Visit https://www.reddit.com/prefs/apps
   - Create a new "script" application
   - Note the client ID and secret

4. **Start the System**:

```bash
# Build and start API server
docker-compose -f docker-compose.api.yml up --build -d

# Access dashboard
open http://localhost:8000
```

## 🚀 Usage Examples

### **Scraping Multiple Subreddits**

```bash
# Start high-volume financial subreddit
curl -X POST http://localhost:8000/scrapers/start \
  -H "Content-Type: application/json" \
  -d '{
    "subreddit": "wallstreetbets",
    "posts_limit": 2000,
    "interval": 180,
    "comment_batch": 30,
    "credentials": {
      "client_id": "your_client_id",
      "client_secret": "your_client_secret",
      "username": "your_username",
      "password": "your_password",
      "user_agent": "RedditScraper/1.0"
    }
  }'

# Start medium-volume investment subreddit
curl -X POST http://localhost:8000/scrapers/start \
  -H "Content-Type: application/json" \
  -d '{
    "subreddit": "investing",
    "posts_limit": 1000,
    "interval": 300,
    "comment_batch": 20,
    "credentials": { ... }
  }'
```

### **Monitoring Operations**

```bash
# Check system health
curl http://localhost:8000/health

# Get scraper statistics
curl http://localhost:8000/scrapers/wallstreetbets/stats

# View recent logs
curl http://localhost:8000/scrapers/wallstreetbets/logs?lines=50

# List all scrapers
curl http://localhost:8000/scrapers
```

## 🎯 Performance Optimization

### **Scraping Efficiency**

- **Bulk Database Operations**: High-performance MongoDB writes
- **Intelligent Deduplication**: Skip existing posts/comments
- **Rate Limit Management**: Automatic Reddit API throttling
- **Memory Efficiency**: Stream processing for large datasets
- **Comment Prioritization**: Focus on active posts first

### **Resource Management**

- **Container Limits**: CPU/memory constraints per scraper
- **Connection Pooling**: Efficient database connections
- **Batch Processing**: Group operations for better throughput
- **Selective Updates**: Only update changed data

## 🔧 Troubleshooting

### **Common Issues**

**❌ Scraper Container Fails Immediately**

```bash
# Check container logs
docker logs reddit-scraper-wallstreetbets

# Common causes:
- Invalid Reddit API credentials
- Missing environment variables
- Network connectivity issues
- Rate limit exceeded
```

**❌ Database Connection Failed**

```bash
# Verify MongoDB URI
# Check IP whitelist in MongoDB Atlas
# Verify database user permissions
```

**❌ Reddit Authentication Error**

```bash
# Verify Reddit app configuration
# Check username/password combination
# Ensure user agent is descriptive and unique
```

### **Debugging Commands**

```bash
# View API logs
docker-compose -f docker-compose.api.yml logs -f

# Check running containers
docker ps

# Inspect container
docker inspect reddit-scraper-wallstreetbets

# Enter container for debugging
docker exec -it reddit-scraper-wallstreetbets bash

# Monitor system resources
docker stats
```

## 📊 Example Output

### **Dashboard Statistics**

```
r/wallstreetbets Statistics:
━━━━━━━━━━━━━━━━━━━━━━
📊 Posts: 15,432
💬 Comments: 2,847,293
✅ Initial Completion: 94.2%
🏢 Metadata: ✓
⏰ Last Updated: 2.3 hours ago
```

### **Scraping Cycle Log**

```
2024-01-20 15:30:45 - reddit-scraper - INFO - SCRAPE CYCLE #127 at 2024-01-20 15:30:45
2024-01-20 15:30:45 - reddit-scraper - INFO - POST SCRAPING PHASE
2024-01-20 15:30:47 - reddit-scraper - INFO - Successfully scraped 2000 posts
2024-01-20 15:30:47 - reddit-scraper - INFO - Bulk operation: 23 new posts, 1977 updated posts
2024-01-20 15:30:47 - reddit-scraper - INFO - COMMENT SCRAPING PHASE
2024-01-20 15:30:48 - reddit-scraper - INFO - Found 30 posts needing comment updates
2024-01-20 15:31:15 - reddit-scraper - INFO - Comment scraping completed: 30 posts (5 initial, 25 updates), 847 new comments
2024-01-20 15:31:15 - reddit-scraper - INFO - CYCLE SUMMARY
2024-01-20 15:31:15 - reddit-scraper - INFO - Posts scraped: 2000 (23 new)
2024-01-20 15:31:15 - reddit-scraper - INFO - Comments processed: 30 posts, 847 new comments
2024-01-20 15:31:15 - reddit-scraper - INFO - Cycle completed in 30.2 seconds
```

## 🏆 Features Summary

### **✨ API Management**

- Web dashboard for all operations
- REST API for programmatic control
- Real-time monitoring and alerts
- Persistent configuration storage

### **🚀 Scalable Scraping**

- Multiple subreddits simultaneously
- Unique credentials per scraper
- Automatic container orchestration
- Smart resource allocation

### **🧠 Intelligent Processing**

- Priority-based comment updates
- Efficient deduplication
- Rate limit management
- Hierarchical comment threading

### **🔒 Enterprise Security**

- Encrypted credential storage
- Container isolation
- Audit logging
- Secure API endpoints

### **📈 Production Ready**

- Automatic failure recovery
- Health monitoring
- Performance optimization
- Comprehensive logging

## 📝 License

MIT License - see LICENSE file for details.

---

**🎯 Ready to start scraping Reddit data at scale? Launch the dashboard at `http://localhost:8000` and begin collecting insights from any subreddit!**
