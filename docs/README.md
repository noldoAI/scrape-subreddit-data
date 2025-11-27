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

Each scraper runs a continuous 3-phase cycle with intelligent prioritization:

### **Phase 1: Posts Scraping** (Every 60 seconds)

**🎯 First-Run Historical Fetch (v1.2+)**:
- **Initial run**: Fetches top posts from last **month** for comprehensive historical data
- **Subsequent runs**: Switches to **daily** "top" posts for ongoing updates
- **Result**: 1000+ historical posts captured automatically on first run

**Multi-Sort Strategy** (100% Coverage):

```
1. Fetch posts using multiple sorting methods:
   - new: ALL posts chronologically (500 limit) - 100% coverage
   - top: Proven quality content (150 limit, time-filtered)
   - rising: Early trending posts (100 limit)

2. Deduplicate posts across sorting methods
3. Extract post metadata (title, score, author, timestamps, etc.)
4. Update existing posts with new scores/comment counts
5. Store new posts discovered from any sorting method
6. Preserve comment tracking status for existing posts

Performance: Complete chronological coverage + quality indicators
API Usage: ~9 API calls per cycle (3 sorting methods)
```

### **Phase 2: Smart Comment Updates** (Continuous, Priority-Based)

**🧠 Intelligent Prioritization**:
```
Priority Queue System:
1. HIGHEST: Posts never scraped (initial scrape) - immediate priority
2. HIGH: High-activity posts (>100 comments) - update every 2 hours
3. MEDIUM: Medium-activity posts (20-100 comments) - update every 6 hours
4. LOW: Low-activity posts (<20 comments) - update every 24 hours
5. Deduplication: Only collect NEW comments, skip existing ones
6. Hierarchical: Preserve parent-child comment relationships
```

**⚡ Comment Depth Limiting** (v1.2+):
```
- Fetches top 4 nesting levels (0-3) by default
- Captures 85-90% of valuable discussion
- Processing: 1-2 minutes vs 30+ minutes for large threads
- Allows 10-15x more posts covered per hour
- Breadth over depth: More posts with meaningful comments
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

Per-Scraper Metrics (Real-time):
📊 Collection Stats:
   ▸ 981 posts (9,547/hr) | ▸ 2,514 comments (24,466/hr)
   Last cycle: 23 posts, 847 comments at 15:31:15
   Total cycles: 127 | Avg cycle: 30.2s
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

**Optimized for 5 scrapers per Reddit account** (v1.2+ with depth limiting)

### **High Activity Subreddits** (wallstreetbets, stocks)

```json
{
  "posts_limit": 150,
  "interval": 60,
  "comment_batch": 12,
  "sorting_methods": ["new", "top", "rising"],
  "sort_limits": {
    "new": 500,
    "top": 150,
    "rising": 100
  }
}
```
*Complete coverage + quality indicators (~21 API calls/min)*

### **Medium Activity Subreddits** (investing, cryptocurrency)

```json
{
  "posts_limit": 100,
  "interval": 60,
  "comment_batch": 12,
  "sorting_methods": ["new", "top", "rising"],
  "sort_limits": {
    "new": 500,
    "top": 150,
    "rising": 100
  }
}
```
*Balanced approach (~21 API calls/min)*

### **Low Activity Subreddits** (pennystocks, niche topics)

```json
{
  "posts_limit": 80,
  "interval": 60,
  "comment_batch": 10,
  "sorting_methods": ["new", "top", "rising"],
  "sort_limits": {
    "new": 500,
    "top": 150,
    "rising": 100
  }
}
```
*Conservative settings (~18 API calls/min)*

**📊 API Usage**: 5 scrapers = ~105 calls/min (within 600/10min Reddit limit)

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
    "interval": 60,
    "comment_batch": 50,
    "sorting_methods": ["new", "hot", "rising"],
    "sort_limits": {
      "new": 1000,
      "hot": 1000,
      "rising": 500
    }
  },
  "credentials": {
    "client_id": "app_client_id",
    "client_secret": "app_secret",
    "username": "reddit_user",
    "password": "user_password",
    "user_agent": "RedditScraper/1.0"
  },
  "metrics": {
    "total_posts_collected": 981,
    "total_comments_collected": 2514,
    "total_cycles": 127,
    "last_cycle_posts": 23,
    "last_cycle_comments": 847,
    "last_cycle_time": "2024-01-20T15:31:15",
    "last_cycle_duration": 30.2,
    "posts_per_hour": 9547,
    "comments_per_hour": 24466,
    "avg_cycle_duration": 30.2
  },
  "auto_restart": true,
  "created_at": "2022-01-20T12:00:00",
  "last_updated": "2022-01-20T12:00:00",
  "restart_count": 0
}
```

## 🔐 Security Features

### **Credential Management**

- Credentials stored in MongoDB (ensure MongoDB is properly secured)
- Use MongoDB Atlas with IP whitelisting and strong passwords
- Enable MongoDB encryption at rest in production
- Masked values in API responses and dashboard (shown as `***`)

### **Container Isolation**

- Each scraper runs in isolated Docker container
- Unique credentials per scraper (no sharing)
- Individual failure containment
- Resource isolation and management
- Environment variables isolated per container

## 📈 Monitoring & Alerting

### **Prometheus + Grafana Monitoring**

Full observability stack with real-time metrics and alerting:

```bash
# Start monitoring stack
docker-compose -f docker-compose.monitoring.yml up -d

# Access dashboards
Grafana:     http://localhost:3000 (admin/admin)
Prometheus:  http://localhost:9090
```

**Grafana Dashboards:**

- **Reddit Scraper Overview**: Live collection metrics, scraper status, failure detection
- **Infrastructure**: CPU, memory, disk, container metrics

**Key Metrics:**

| Metric | Description |
|--------|-------------|
| `reddit_scraper_posts_total{subreddit}` | Total posts per subreddit |
| `reddit_scraper_comments_total{subreddit}` | Total comments per subreddit |
| `reddit_scraper_status{subreddit}` | Scraper status (1=running, 0=stopped, -1=failed) |
| `reddit_scraper_posts_per_hour{subreddit}` | Collection rate |
| `reddit_scraper_errors_unresolved{subreddit}` | Unresolved errors |

**Live Collection Panels:**

- Posts/Comments collected in last 10 minutes and 1 hour
- Collection rate over time per subreddit (spot failures instantly)
- Failed/stopped scrapers table with highlighting
- Scraper status history timeline

**Alerting (Telegram):**

```bash
# Set environment variables for Telegram alerts
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id

# Alerts include:
# - ScraperDown: Scraper failed for 5+ minutes
# - RateLimitCritical: API quota < 50 remaining
# - NoPostsCollected: No posts in 30 minutes
# - HighCPU/HighMemory/DiskSpaceLow
```

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

## 🔧 Production Deployment

### **Rootless Docker Persistence Issue**

If you're running Docker in rootless mode (common on cloud VMs), containers will stop when you log out of SSH. This happens because rootless Docker runs as a user service that terminates when the user session ends.

**Symptoms:**
- Containers run fine while SSH'd in
- Containers stop/fail when you log out
- API and scrapers don't persist after logout

**Solution: Enable User Lingering**

```bash
# Enable persistent user session (keeps Docker running after logout)
sudo loginctl enable-linger $USER

# Verify linger is enabled
loginctl show-user $USER | grep Linger
# Should show: Linger=yes

# Check Docker service persists
systemctl --user status docker.service
```

**Why This Works:**
- `loginctl enable-linger` keeps your user systemd instance running
- Docker daemon (user service) stays active even after SSH logout
- All containers continue running independently of SSH sessions

**Verification:**
```bash
# 1. Start containers
docker ps

# 2. Log out of SSH completely

# 3. Wait 2-5 minutes

# 4. Log back in and check
docker ps
# Containers should still be running
```

**Important:** This fix is **required** for production deployments using rootless Docker. Without it, your scrapers will only work during active SSH sessions.

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

### **Scraping Efficiency** (v1.2+)

- **100% Coverage**: "new" sorting captures ALL posts chronologically
- **Smart Deduplication**: Skip duplicate posts across sorting methods
- **Optimized API Usage**: ~21 calls/min per scraper (5 scrapers = ~105 calls/min)
- **Comment Depth Limiting**: 10-15x faster processing (top 4 nesting levels only)
- **First-Run Optimization**: Automatic month-long historical fetch on first run
- **Bulk Database Operations**: High-performance MongoDB writes
- **Rate Limit Management**: Automatic Reddit API throttling
- **Memory Efficiency**: Stream processing for large datasets
- **Intelligent Prioritization**: High-engagement posts get updated more frequently
- **Real-time Metrics**: Track collection rates (posts/hr, comments/hr)

### **Resource Management**

- **Container Limits**: CPU/memory constraints per scraper
- **Connection Pooling**: Efficient database connections
- **Batch Processing**: Group operations for better throughput
- **Selective Updates**: Only update changed data

## 🔧 Troubleshooting

### **Common Issues**

**❌ Containers Stop After SSH Logout (Rootless Docker)**

```bash
# SOLUTION: Enable user lingering
sudo loginctl enable-linger $USER

# Verify
loginctl show-user $USER | grep Linger=yes

# This is REQUIRED for production deployments with rootless Docker
# See "Production Deployment" section above for details
```

**❌ Scraper Container Fails Immediately**

```bash
# Check container logs
docker logs reddit-scraper-wallstreetbets

# Common causes:
- Invalid Reddit API credentials
- Missing environment variables
- Network connectivity issues
- Rate limit exceeded
- Docker image not built (run: docker build -f Dockerfile -t reddit-scraper .)
```

**❌ Database Connection Failed**

```bash
# Verify MongoDB URI
# Check IP whitelist in MongoDB Atlas
# Verify database user permissions
# Test connection: docker logs reddit-scraper-api
```

**❌ Reddit Authentication Error**

```bash
# Verify Reddit app configuration
# Check username/password combination
# Ensure user agent is descriptive and unique
# Check for 2FA (may cause oauth invalid_grant errors)
```

**❌ Dashboard Not Showing Scrapers**

```bash
# Rebuild API container with latest fixes
docker compose -f docker-compose.api.yml down
docker compose -f docker-compose.api.yml build --no-cache reddit-scraper-api
docker compose -f docker-compose.api.yml up -d

# Check browser console for JavaScript errors
# Verify /scrapers endpoint returns data: curl http://localhost:8000/scrapers
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
