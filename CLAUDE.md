# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Reddit scraping system** with two deployment modes:
1. **API Management Mode** (Primary): Web dashboard + REST API for managing multiple containerized scrapers
2. **Standalone Mode**: Direct Python script execution for single-subreddit scraping

**Key Architecture**: Each subreddit scraper runs in its own Docker container with isolated Reddit API credentials to avoid rate limit conflicts. All scrapers share a MongoDB database.

## Project Structure

```
scrape-subreddit-data/
├── api.py                    # FastAPI management server (main entry point)
├── posts_scraper.py          # Posts scraping engine (runs in Docker containers)
├── comments_scraper.py       # Comments scraping engine (runs in Docker containers)
├── config.py                 # Centralized configuration
├── rate_limits.py            # Reddit API rate limiting utility
├── embedding_worker.py       # Background embedding worker
│
├── api/                      # API module helpers (partial extraction)
│   ├── models.py             # Pydantic models
│   └── services/
│       └── encryption.py     # Credential encryption
│
├── discovery/                # Semantic search & subreddit discovery
│   ├── discover_subreddits.py    # Search Reddit for subreddits
│   ├── generate_embeddings.py    # Generate semantic embeddings
│   ├── llm_enrichment.py         # LLM-based audience profiling
│   ├── enrich_existing.py        # Batch enrich existing subreddits
│   ├── test_persona_search.py    # Test persona-based search
│   ├── semantic_search.py        # CLI semantic search tool
│   └── setup_vector_index.py     # MongoDB vector index setup
│
├── tools/                    # Maintenance utilities
│   └── repair_ghost_posts.py # Data integrity repair
│
├── docs/                     # Documentation
├── Dockerfile               # Scraper container image
├── Dockerfile.api           # API server container image
└── docker-compose*.yml      # Container orchestration
```

## Common Commands

### API Management Mode (Recommended)

```bash
# Build the scraper Docker image (required first!)
docker build -f Dockerfile -t reddit-scraper .

# Start the API server
docker-compose -f docker-compose.api.yml up -d

# View API logs
docker-compose -f docker-compose.api.yml logs -f

# Stop API server
docker-compose -f docker-compose.api.yml down

# Access web dashboard
# http://localhost:8000 (or port 80 if using the default docker-compose config)

# List all scraper containers
docker ps --filter "name=reddit-posts-scraper-"
docker ps --filter "name=reddit-comments-scraper-"

# View logs from a specific scraper container
docker logs reddit-posts-scraper-wallstreetbets
docker logs reddit-comments-scraper-wallstreetbets

# Stop all scraper containers
docker stop $(docker ps --filter "name=reddit-posts-scraper-" -q)
docker stop $(docker ps --filter "name=reddit-comments-scraper-" -q)
```

### Standalone Mode (Alternative)

```bash
# Install dependencies
pip install -r requirements.txt

# Run POSTS scraper for a specific subreddit
python posts_scraper.py SUBREDDIT_NAME --posts-limit 1000 --interval 300

# Run COMMENTS scraper for a specific subreddit
python comments_scraper.py SUBREDDIT_NAME --interval 300 --comment-batch 12

# Show statistics only
python posts_scraper.py SUBREDDIT_NAME --stats
python comments_scraper.py SUBREDDIT_NAME --stats

# Update subreddit metadata only
python posts_scraper.py SUBREDDIT_NAME --metadata-only

# Using Docker Compose with standalone mode (runs both scrapers)
docker-compose --env-file .env up -d
docker-compose logs -f
docker-compose down
```

## Code Architecture

### Core Components

1. **`api.py`** - FastAPI management server
   - Creates/manages Docker containers for posts and comments scrapers
   - Stores scraper configurations and encrypted credentials in MongoDB (`reddit_scrapers` collection)
   - Provides REST API endpoints and web dashboard
   - Monitors container health and auto-restarts failed scrapers
   - Supports `scraper_type` field: "posts" or "comments"

2. **`posts_scraper.py`** - Posts scraping engine (runs in containers)
   - Continuous loop handling two phases:
     - **Phase 1**: Posts scraping (multi-sort: new, top, rising)
     - **Phase 2**: Subreddit metadata (every 24 hours)
   - Uses PRAW library to interact with Reddit API
   - First-run historical fetch (month of top posts)

3. **`comments_scraper.py`** - Comments scraping engine (runs in containers)
   - Continuous loop for comment scraping with intelligent prioritization:
     - **HIGHEST**: Posts never scraped (initial scrape)
     - **HIGH**: High-activity posts (>100 comments) - every 2 hours
     - **MEDIUM**: Medium-activity posts (20-100 comments) - every 6 hours
     - **LOW**: Low-activity posts (<20 comments) - every 24 hours
   - Depth-limited scraping (top 3 levels for efficiency)
   - Deduplication to avoid re-scraping existing comments

4. **`config.py`** - Centralized configuration
   - Database name: `"noldo"`
   - Collection names: `reddit_posts`, `reddit_comments`, `subreddit_metadata`, `reddit_scrapers`, `reddit_accounts`
   - Separate configs: `DEFAULT_POSTS_SCRAPER_CONFIG` and `DEFAULT_COMMENTS_SCRAPER_CONFIG`
   - Docker, API, monitoring, and security configurations

5. **`rate_limits.py`** - Reddit API rate limiting
   - Monitors Reddit API quota (remaining/used requests)
   - Automatically pauses when rate limit is low
   - Waits for rate limit reset when necessary

### Data Flow Architecture

```
Web Dashboard/API Request
         ↓
    api.py creates Docker container(s)
         ↓
   ┌──────────────────────────────────────────────────┐
   │   POSTS SCRAPER CONTAINER                         │
   │   (reddit-posts-scraper-{subreddit})              │
   │   └── posts_scraper.py                            │
   │       1. Scrape posts → MongoDB (reddit_posts)    │
   │       2. Update metadata → MongoDB (subreddit_*)  │
   └──────────────────────────────────────────────────┘
   ┌──────────────────────────────────────────────────┐
   │   COMMENTS SCRAPER CONTAINER                      │
   │   (reddit-comments-scraper-{subreddit})           │
   │   └── comments_scraper.py                         │
   │       Priority-based comment scraping             │
   │       → MongoDB (reddit_comments)                 │
   └──────────────────────────────────────────────────┘
         ↓
    Container status tracked in MongoDB (reddit_scrapers)
         ↓
    API monitors health & auto-restarts on failure
```

### MongoDB Collections Schema

**`reddit_posts`**:
- `post_id` (unique index)
- Post metadata (title, url, score, author, timestamps, etc.)
- Comment tracking fields: `comments_scraped`, `initial_comments_scraped`, `last_comment_fetch_time`

**`reddit_comments`**:
- `comment_id` (unique index)
- `post_id` (index) - links to parent post
- `parent_id` (index) - links to parent comment or null
- Hierarchical comment structure with `depth` field
- Comment metadata (body, score, author, timestamps, etc.)

**`subreddit_metadata`**:
- `subreddit_name` (unique index)
- Subscriber counts, settings, rules, visual elements
- `last_updated` - tracks when to update (24h interval)

**`reddit_scrapers`**:
- `subreddit` (unique) - identifies scraper
- `container_id`, `container_name` - Docker container info
- `status` - running/stopped/failed
- `config` - posts_limit, interval, comment_batch
- `credentials` - encrypted Reddit API credentials
- `auto_restart` - automatic failure recovery flag

**`reddit_accounts`**:
- Stores Reddit account credentials for reuse across scrapers

**`reddit_scrape_errors`**:
- `subreddit` - subreddit where error occurred
- `post_id` - post that failed to scrape
- `error_type` - type of error (comment_scrape_failed, verification_failed, etc.)
- `error_message` - detailed error message
- `retry_count` - number of retries attempted
- `timestamp` - when error occurred
- `resolved` - whether error has been fixed

### Data Integrity Features (v1.1+)

**Problem Solved**: Earlier versions had a critical bug where posts were marked as `comments_scraped: True` even when comment scraping failed, resulting in "ghost" posts with zero comments in the database.

**Solution Implemented**:
1. **Verification Before Marking**: Comments are verified in the database before setting `comments_scraped: True`
2. **Improved Error Handling**: Failed scrapes are logged to `reddit_scrape_errors` collection and NOT marked as complete
3. **Optimized Comment Fetching**: Depth-limited scraping captures 85-90% of valuable discussion in a fraction of the time
4. **Retry Logic**: Automatic retry with exponential backoff for transient failures
5. **Repair Script**: `repair_ghost_posts.py` identifies and fixes existing corrupted data

**Configuration Options** (in config.py):
- `replace_more_limit`: `0` = skip MoreComments (fastest), `None` = expand all (slowest), or set integer limit (default: `0`)
- `max_comment_depth`: Maximum nesting level to fetch, 0-indexed (default: `3` = levels 0,1,2,3 = top 4 levels)
- `posts_per_comment_batch`: Number of posts to process per cycle (default: `12`, increased due to faster depth-limited processing)
- `top_time_filter`: Time filter for regular "top" scraping (default: `"day"`)
- `initial_top_time_filter`: Time filter for first run to get historical data (default: `"month"`)
- `max_retries`: Number of retry attempts for failed operations (default: `3`)
- `retry_backoff_factor`: Exponential backoff multiplier (default: `2` = 2s, 4s, 8s)
- `verify_before_marking`: Enable verification step before marking posts scraped (default: `True`)

**Repair Utility** ([tools/repair_ghost_posts.py](tools/repair_ghost_posts.py)):
```bash
# Show statistics about data integrity issues
python tools/repair_ghost_posts.py --stats-only

# Show what would be repaired (dry run)
python tools/repair_ghost_posts.py --dry-run

# Actually repair ghost posts
python tools/repair_ghost_posts.py

# Repair specific subreddit
python tools/repair_ghost_posts.py --subreddit wallstreetbets

# Also repair incomplete posts (missing >10% of comments)
python tools/repair_ghost_posts.py --include-incomplete
```

### First-Run Historical Fetch (v1.2+)

**Automatic Historical Data Collection**: When scraping a subreddit for the first time, the system automatically fetches historical posts to build a comprehensive dataset.

**How It Works:**
1. **First Run Detection**: System checks if subreddit has any posts in database
2. **Initial Fetch**: Uses `initial_top_time_filter: "month"` to get top posts from last 30 days
3. **Subsequent Runs**: Switches to `top_time_filter: "day"` for daily updates

**Timeline:**
- **Posts appear**: 1-2 minutes (immediately visible in database)
- **Full comment scraping**: 1-3 hours (gradual, prioritized by engagement)
- **Result**: 1000+ historical posts with complete data

**Benefits:**
- ✅ Comprehensive coverage from day one
- ✅ No API spike (gradual comment scraping)
- ✅ Prioritizes high-engagement posts first
- ✅ Seamless transition to daily updates

**Example:**
```
First cycle:  "Using 'month' time filter for historical data" → 1000+ posts
Second cycle: "Using 'day' time filter" → ~25 new daily posts
```

### Multi-Subreddit Mode (v1.5+)

Scrape up to **30 subreddits with 1 Reddit account** in a single container using rotation.

**Key Features:**
- 1 container handles multiple subreddits in rotation
- First run: fetches maximum historical posts (month of top posts)
- Subsequent runs: only new posts (upsert deduplication)
- Dashboard support with mode selector

**CLI Usage:**
```bash
# Single subreddit (backwards compatible)
python posts_scraper.py wallstreetbets --posts-limit 100 --interval 60

# Multi-subreddit rotation (NEW)
python posts_scraper.py stocks,investing,wallstreetbets --posts-limit 50 --interval 300

# Show stats for multiple subreddits
python posts_scraper.py stocks,investing --stats
```

**API Usage:**
```bash
POST /scrapers/start-flexible
{
    "subreddits": ["stocks", "investing", "wallstreetbets", "options", "stockmarket"],
    "scraper_type": "posts",
    "posts_limit": 50,
    "interval": 300,
    "saved_account_name": "my_account"
}
```

**Dashboard:**
- Select "Multi-Subreddit (up to 30)" from Mode dropdown
- Enter comma-separated subreddit names
- Container named: `reddit-posts-scraper-multi-5subs-stocks`

**Recommended Config for 30 Subreddits:**
- `posts_limit`: 50 per subreddit
- `interval`: 300 seconds (5 min between full rotations)
- `sorting_methods`: ["new", "top"] (reduce from 3 to 2)

**Rate Limit Analysis:**

| Mode | Subreddits | API Calls/Cycle | Safe? |
|------|------------|-----------------|-------|
| Single | 1 | ~12 calls | Yes |
| Multi | 30 | ~210-300 calls | Yes (under 600/10min) |

**How Rotation Works:**
1. Process subreddit 1: scrape posts, save to DB, update metadata
2. Brief pause (2 seconds)
3. Process subreddit 2: scrape posts, save to DB, update metadata
4. ... repeat for all subreddits
5. Wait for interval (e.g., 300 seconds)
6. Start next rotation cycle

**Error Handling:**
- If one subreddit fails, continues with next (try/catch per subreddit)
- Errors logged but don't stop the rotation
- Cycle summary shows processed/total and error count

**Configuration** (config.py):
```python
MULTI_SCRAPER_CONFIG = {
    "max_subreddits_per_container": 30,
    "rotation_delay": 2,              # Seconds between subreddits
    "recommended_posts_limit": 50,
    "recommended_interval": 300,
}
```

### Smart Comment Update Prioritization

The system uses intelligent priority-based comment updates based on post activity:

**Priority Tiers:**
1. **HIGHEST**: Posts never scraped (initial scrape) - immediate priority
2. **HIGH**: High-activity posts (>100 comments) - update every **2 hours**
3. **MEDIUM**: Medium-activity posts (20-100 comments) - update every **6 hours**
4. **LOW**: Low-activity posts (<20 comments) - update every **24 hours**

**Sorting Order:**
1. Unscraped posts first
2. Then by comment count (highest first) - prioritizes active discussions
3. Then by creation time (newest first)

**Comment Depth Limiting** (v1.2+):
- Fetches only top 3 nesting levels by default (levels 0-3)
- Captures 85-90% of valuable discussion (deep nests are often low-value debates)
- Processing time: **1-2 minutes** instead of 30+ minutes for large threads
- Allows processing **10-15x more posts** per hour
- `replace_more_limit: 0` skips "load more comments" expansion (70-80% fewer API calls)

**Benefits:**
- Hot discussion threads (500+ comments) get checked 3x more frequently
- Low-activity posts (<20 comments) save API calls by checking less often
- Automatically adapts to post engagement levels
- Deduplication: Only collects NEW comments, skips existing ones
- **Breadth over depth**: More posts covered with meaningful comments from each

Query uses `$or` conditions with `initial_comments_scraped`, `num_comments`, and `last_comment_fetch_time` to determine priority.

### Container Isolation Strategy

- Each subreddit can have two Docker containers:
  - Posts: `reddit-posts-scraper-{subreddit}`
  - Comments: `reddit-comments-scraper-{subreddit}`
- Containers have unique Reddit API credentials (stored encrypted in MongoDB)
- Prevents rate limit conflicts between scrapers
- API server mounts Docker socket (`/var/run/docker.sock`) to manage containers
- Containers are created with environment variables passed via command-line

### Security Features

- **Credential Encryption**: Uses Fernet (symmetric encryption) with key stored in `/tmp/.scraper_key`
- **Encrypt on write**: Credentials encrypted before MongoDB storage (see `encrypt_credential()` in api.py)
- **Decrypt on read**: Decrypted when launching containers (see `decrypt_credential()` in api.py)
- **Masked in API responses**: Credentials shown as `"***"` in API responses
- **Container isolation**: Each scraper has isolated credentials and process space

## Environment Configuration

### Required Environment Variables

Create a `.env` file:

```bash
# MongoDB Connection (shared across all scrapers)
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/dbname

# Optional: Default Reddit API credentials for API server
R_CLIENT_ID=your_client_id
R_CLIENT_SECRET=your_client_secret
R_USERNAME=your_username
R_PASSWORD=your_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
```

**Note**: Each scraper container gets its own credentials provided via the API, not from .env file.

### Per-Subreddit Configuration

For standalone mode, create separate env files (e.g., `.env.wallstreetbets`):

```bash
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=2000
SCRAPE_INTERVAL=180
COMMENT_BATCH=30

# Reddit credentials for this specific scraper
R_CLIENT_ID=app1_client_id
R_CLIENT_SECRET=app1_secret
R_USERNAME=reddit_user1
R_PASSWORD=password1
R_USER_AGENT=WSBScraper/1.0 by User1

MONGODB_URI=mongodb+srv://...
```

## Important Implementation Details

### When Modifying Comment Scraping Logic

- **Preserve tracking fields**: When updating posts in `save_posts_to_db()`, existing comment tracking fields (`comments_scraped`, `initial_comments_scraped`, `last_comment_fetch_time`) MUST be preserved
- **Update tracking after scraping**: Always call `mark_posts_comments_updated()` after successfully scraping comments
- **Deduplication is critical**: Use `get_existing_comment_ids()` to avoid re-scraping existing comments
- **Verify before marking** (v1.1+): Only mark posts as scraped AFTER verifying comments are in the database
- **Error logging**: Log failures to `reddit_scrape_errors` collection for tracking and debugging
- **Complete pagination**: Use `replace_more(limit=None)` to capture all nested comments, not just top-level

### When Adding New Scraper Features

- **Update config.py first**: Add new configuration options to `DEFAULT_SCRAPER_CONFIG` or other relevant config dictionaries
- **Container restart required**: Changes to posts_scraper.py or comments_scraper.py require rebuilding Docker image and restarting containers
- **API changes**: If modifying API endpoints, update both the endpoint handler and the HTML dashboard in api.py

### Rate Limiting Best Practices

- Always call `check_rate_limit(reddit)` before making Reddit API calls
- Current threshold: pauses when <50 requests remaining
- Add `time.sleep(2)` between comment scraping to be respectful

### Docker Container Lifecycle

1. API receives start request with credentials
2. Credentials encrypted and stored in MongoDB
3. Container created with environment variables (decrypted credentials)
4. Container runs `posts_scraper.py` or `comments_scraper.py` with command-line args
5. Health check monitors container every 30 seconds
6. If failed and `auto_restart=True`, API restarts container after cooldown
7. Container logs accessible via `docker logs {container_name}` or API endpoint

## Testing & Debugging

### Local Development Without Docker

```bash
# Set environment variables
export R_CLIENT_ID=...
export R_CLIENT_SECRET=...
export R_USERNAME=...
export R_PASSWORD=...
export R_USER_AGENT=...
export MONGODB_URI=...

# Run scraper directly
python posts_scraper.py wallstreetbets --stats
python posts_scraper.py wallstreetbets --posts-limit 100 --interval 60

python comments_scraper.py wallstreetbets --stats
python comments_scraper.py wallstreetbets --interval 60 --comment-batch 5
```

### Debugging Container Issues

```bash
# Check if container is running
docker ps -a --filter "name=reddit-posts-scraper-wallstreetbets"
docker ps -a --filter "name=reddit-comments-scraper-wallstreetbets"

# View container logs
docker logs reddit-posts-scraper-wallstreetbets --tail 100
docker logs reddit-comments-scraper-wallstreetbets --tail 100

# Inspect container details
docker inspect reddit-posts-scraper-wallstreetbets

# Enter running container for debugging
docker exec -it reddit-posts-scraper-wallstreetbets bash

# Check resource usage
docker stats reddit-posts-scraper-wallstreetbets
```

### Common Issues

**Container exits immediately**:
- Check logs: `docker logs reddit-posts-scraper-{subreddit}` or `docker logs reddit-comments-scraper-{subreddit}`
- Verify credentials are correct
- Check MongoDB connection string
- Ensure `reddit-scraper` image exists: `docker images | grep reddit-scraper`

**Rate limit errors**:
- Each Reddit app gets ~600 requests per 10 minutes
- Multiple scrapers sharing credentials will hit limits faster
- Solution: Use unique credentials per scraper

**Database connection errors**:
- Verify MongoDB URI is correct
- Check IP whitelist in MongoDB Atlas
- Test connection: `python -c "import pymongo; pymongo.MongoClient('YOUR_URI').admin.command('ping')"`

**Ghost posts (marked scraped with zero comments)**:
- **Symptom**: Posts have `comments_scraped: True` but no comments in database
- **Cause**: Fixed in v1.1+ - earlier versions had a bug where posts were marked before verifying comments were saved
- **Solution**: Run `python tools/repair_ghost_posts.py` to identify and fix affected posts
- **Prevention**: Ensure `verify_before_marking: True` in config (default in v1.1+)

**Incomplete comment data (missing comments)**:
- **Symptom**: Post has fewer comments in DB than `num_comments` field indicates
- **Cause (v1.2+)**: Intentional depth limiting (`max_comment_depth: 3`) - captures top 3 levels only for speed
- **Cause (v1.1)**: Earlier versions used `replace_more(limit=10)` which missed deeply nested comments
- **Solution**: This is expected behavior in v1.2+ (breadth over depth strategy). For v1.1 data: `python tools/repair_ghost_posts.py --include-incomplete`
- **Note**: v1.2+ prioritizes covering more posts with meaningful comments over capturing every deeply nested reply

**Verification failures in logs**:
- **Symptom**: Logs show "VERIFICATION FAILED - 0 comments in DB"
- **Cause**: Comments failed to save to database (network issue, MongoDB problem)
- **Action**: Check `reddit_scrape_errors` collection for details
- **Resolution**: Failed posts will automatically retry on next cycle

## Azure VM Deployment

### Connecting to Azure VM from Local Machine

The production system runs on an Azure VM (`noldo-data-server`) in the West US 2 region.

**VM Details:**
- **Resource Group**: `noldo-data-server`
- **VM Name**: `noldo-data-server`
- **Public IP**: `20.64.246.60`
- **Username**: `azureuser`
- **SSH Key**: `~/.ssh/noldo-data-server-key.pem` (created during VM setup)

**Connect via SSH:**

```bash
# Direct SSH connection (recommended)
ssh -i ~/.ssh/noldo-data-server-key.pem azureuser@20.64.246.60

# Or create SSH config for easier access
# Add to ~/.ssh/config:
Host noldo-azure
    HostName 20.64.246.60
    User azureuser
    IdentityFile ~/.ssh/noldo-data-server-key.pem

# Then connect with:
ssh noldo-azure
```

**Using Azure CLI:**

```bash
# Install Azure CLI (if not installed)
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Login to Azure
az login

# Connect using Azure CLI (alternative method)
az ssh vm --resource-group noldo-data-server --name noldo-data-server

# Get VM status
az vm show --resource-group noldo-data-server --name noldo-data-server --query "powerState" -o tsv

# Start/stop VM
az vm start --resource-group noldo-data-server --name noldo-data-server
az vm stop --resource-group noldo-data-server --name noldo-data-server
```

**Common VM Operations:**

```bash
# After connecting, check system status
htop                    # Monitor CPU/memory
docker ps               # List running containers
docker stats            # Container resource usage
df -h                   # Disk usage
systemctl status docker # Docker daemon status

# Update system packages
sudo apt update
sudo apt upgrade

# Reboot if system updates require it
sudo reboot
```

## API Endpoints Reference

### Scraper Management
- `POST /scrapers/start` - Start new scraper container
- `GET /scrapers` - List all scrapers (includes database totals + scraper metrics)
- `GET /scrapers/{subreddit}/status` - Container status
- `POST /scrapers/{subreddit}/stop` - Stop container
- `POST /scrapers/{subreddit}/restart` - Restart container
- `DELETE /scrapers/{subreddit}` - Remove scraper and config
- `GET /scrapers/{subreddit}/logs?lines=100` - Container logs

### Statistics & Analytics
- `GET /scrapers/{subreddit}/stats` - Comprehensive subreddit statistics
  - Returns: posts/comments counts, date ranges, averages, content breakdown
  - Add `?detailed=true` for top posts, top authors, and distributions
- `GET /stats/global` - Cross-subreddit statistics
  - Returns: total posts/comments across all subreddits, per-subreddit breakdown, system-wide metrics

**Statistics Include:**
- **Basic Counts**: Total posts, total comments, database totals
- **Data Coverage**: Completion rates, date ranges, recent activity (24h)
- **Content Stats**: Averages (comments/post, scores, upvote ratios), self vs link posts, NSFW/locked counts, posts by sorting method
- **Comment Stats**: Average scores, max depth, gilded/awarded counts, top-level vs reply breakdown
- **Scraper Metrics**: Uptime, collection rates, cycle statistics, restart counts
- **Error Tracking**: Total errors, unresolved errors, error types, recent errors
- **Subreddit Metadata**: Subscribers, active users, subreddit age, NSFW status
- **Detailed Analytics** (with `?detailed=true`): Top 10 posts by score, most commented posts, top authors

### System Monitoring
- `GET /health` - System health (database + Docker status)
- `GET /presets` - Configuration presets for different subreddit types
- `POST /scrapers/restart-all-failed` - Restart all failed containers
- `GET /metrics` - Prometheus metrics endpoint

## Prometheus + Grafana Monitoring

Full observability stack with real-time metrics, dashboards, and alerting.

### Quick Start

```bash
# Start monitoring stack
docker-compose -f docker-compose.monitoring.yml up -d

# Access dashboards
Grafana:     http://localhost:3000 (admin/admin)
Prometheus:  http://localhost:9090
```

### Components

| Service | Port | Description |
|---------|------|-------------|
| Prometheus | 9090 | Metrics collection and storage |
| Grafana | 3000 | Dashboards and visualization |
| Alertmanager | 9093 | Alert routing (Telegram) |
| Node Exporter | 9100 | Host metrics (CPU, memory, disk) |
| cAdvisor | 8080 | Container metrics |

### Grafana Dashboards

**Reddit Scraper Overview** (`overview.json`):
- Row 1: Total posts, comments, active scrapers, errors, API/DB status
- Row 2: Posts/comments collected in last 10m and 1h (live stats)
- Row 3: Collection rate over time per subreddit (spot failures instantly)
- Row 4: Failed/stopped scrapers table, status history timeline
- Row 5: All scrapers status table

**Infrastructure** (`infrastructure.json`):
- CPU, memory, disk usage gauges
- CPU/memory over time graphs
- Container CPU/memory per scraper
- Network I/O

### Key Prometheus Metrics

```promql
# Per-subreddit metrics
reddit_scraper_posts_total{subreddit="X"}       # Total posts in DB
reddit_scraper_comments_total{subreddit="X"}    # Total comments in DB
reddit_scraper_status{subreddit="X"}            # 1=running, 0=stopped, -1=failed
reddit_scraper_posts_per_hour{subreddit="X"}    # Collection rate
reddit_scraper_errors_unresolved{subreddit="X"} # Unresolved errors

# Live collection (use increase() for activity)
sum(increase(reddit_scraper_posts_total[10m]))  # Posts collected last 10 min
increase(reddit_scraper_posts_total[5m])        # Per-subreddit collection rate

# System metrics
reddit_scraper_up                               # API health (1=up, 0=down)
reddit_database_connected                       # MongoDB status
reddit_scrapers_active{scraper_type="posts"}    # Active scraper count
```

### Alerting (Telegram)

Set environment variables for Telegram alerts:

```bash
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

**Alert Rules** (`monitoring/alerts.yml`):
- `ScraperDown`: Scraper failed for 5+ minutes
- `RateLimitCritical`: API quota < 50 remaining
- `NoPostsCollected`: No posts in 30 minutes
- `HighErrorRate`: >10 unresolved errors
- `HighCPU`: CPU > 80% for 15 minutes
- `HighMemory`: Memory > 85% for 15 minutes
- `DiskSpaceLow`: Disk < 15% free

### Monitoring Files

| File | Description |
|------|-------------|
| `docker-compose.monitoring.yml` | Monitoring stack compose |
| `monitoring/prometheus.yml` | Prometheus scrape config |
| `monitoring/alerts.yml` | Alert rules |
| `monitoring/alertmanager.yml` | Alertmanager config |
| `monitoring/grafana/dashboards/overview.json` | Main dashboard |
| `monitoring/grafana/dashboards/infrastructure.json` | Host/container metrics |
| `metrics.py` | Prometheus metrics module |

## Configuration Presets

The system includes presets optimized for **5 scrapers per Reddit account** (v1.2+ with depth limiting):

- **High Activity** (wallstreetbets, stocks): posts_limit=150, interval=60s, comment_batch=12, sorting=["new", "top", "rising"]
- **Medium Activity** (investing, crypto): posts_limit=100, interval=60s, comment_batch=12, sorting=["new", "top", "rising"]
- **Low Activity** (pennystocks, niche): posts_limit=80, interval=60s, comment_batch=10, sorting=["new", "top", "rising"]

These are defined in api.py and accessible via `GET /presets` endpoint.

**Note**: Comment batch sizes increased in v1.2 due to 10-15x faster processing with depth limiting.

## Rate Limit Optimization Strategy

**Reddit API Limits**: ~600 requests per 10 minutes per OAuth app (~60 requests/minute)

**Design Philosophy**: System is optimized to allow **5 subreddit scrapers per Reddit account**, maximizing efficiency while staying within Reddit's rate limits.

**Sorting Focus**:
- **"new"**: Captures ALL posts chronologically (100% coverage)
- **"top" (day)**: Captures proven quality content from the last 24 hours
- **"rising"**: Catches early trending posts before they peak
- This combination ensures complete coverage with quality indicators

**API Usage per Scraper** (with default config):
- Post scraping: ~9 API calls (3 sorting methods × 3 calls each)
- Comment scraping: ~12 API calls (6 posts × 2 calls)
- **Total: ~21 API calls per minute**
- **5 scrapers = ~105 calls/min** (stays within 600/10min limit with buffer)

**Key Configuration Options**:
- `sorting_methods`: `["new", "top", "rising"]` - Complete coverage + quality indicators
- `top_time_filter`: `"day"` - Time window for "top" sorting (hour/day/week/month/year/all)
- `posts_limit`: `100` - Default limit (can override per sorting method)
- `sort_limits`: Per-method overrides (`{"new": 500, "top": 150, "rising": 100}`)
- `posts_per_comment_batch`: `6` - Comments processed per cycle

**To customize** (via dashboard or command line):
```bash
python posts_scraper.py subreddit \
  --posts-limit 150 \
  --comment-batch 8 \
  --sorting-methods "new,top,rising"
```

## Semantic Subreddit Search (v1.3+)

The system includes a **semantic search engine** for discovering relevant subreddits using natural language queries rather than keyword matching.

### **Overview**

Search for subreddits by meaning: `"building b2b saas"` → finds r/SaaS, r/startups, r/Entrepreneur

**Technology Stack**:
- **Embedding Model**: Azure OpenAI `text-embedding-3-small` (1536 dimensions)
- **Vector Storage**: MongoDB Atlas Vector Search (HNSW indexing)
- **Library**: `openai` (Azure OpenAI SDK)
- **Cost**: ~$0.00002 per 1K tokens (very low cost API)

### **Key Features**

1. **Semantic Understanding**: Finds relevant subreddits even without exact keyword matches
2. **Context-Rich Embeddings**: Uses rules, guidelines, descriptions, and sample posts
3. **Hybrid Search**: Combines semantic similarity with metadata filters (subscribers, NSFW, language)
4. **Lightweight Deployment**: No local ML models, ~200MB Docker image (vs ~1.5GB with PyTorch)

### **Usage**

#### **1. Discover Subreddits by Topic**

```bash
# Search Reddit and scrape comprehensive metadata
python discovery/discover_subreddits.py --query "saas" --limit 50

# Multiple queries at once
python discovery/discover_subreddits.py --query "startup,entrepreneur,business" --limit 50
```

**What it collects**:
- Basic metadata (title, description, subscribers, etc.)
- Community rules (topic indicators)
- Post guidelines (detailed context)
- Sample posts (top 20 from last month)

#### **2. Generate Embeddings**

```bash
# Generate embeddings for all discovered subreddits
python discovery/generate_embeddings.py --batch-size 32

# Force regenerate embeddings
python discovery/generate_embeddings.py --force

# Check embedding statistics
python discovery/generate_embeddings.py --stats
```

**Performance** (Azure OpenAI API):
- 10 subreddits: ~5 seconds
- 100 subreddits: ~30 seconds
- 1000 subreddits: ~5 minutes

#### **3. Setup Vector Search Index**

```bash
# Create MongoDB Atlas vector search index (one-time setup)
python discovery/setup_vector_index.py

# Verify index is working
python discovery/setup_vector_index.py --verify-only

# Recreate index
python discovery/setup_vector_index.py --drop
```

**Index creation** takes 1-5 minutes. Requires MongoDB Atlas (M0+ free tier supported).

#### **4. Semantic Search**

```bash
# Search by natural language query
python discovery/semantic_search.py --query "building b2b saas" --limit 10

# With filters
python discovery/semantic_search.py --query "crypto trading" \
  --limit 20 \
  --min-subscribers 10000 \
  --include-nsfw

# Interactive mode
python discovery/semantic_search.py --interactive
```

**Search Filters**:
- `--min-subscribers`: Minimum subscriber count (default: 1000)
- `--max-subscribers`: Maximum subscriber count
- `--include-nsfw`: Include NSFW subreddits
- `--language`: Language filter (e.g., "en")
- `--type`: Subreddit type (public/private/restricted)

### **REST API Endpoints**

#### **Semantic Search**
```bash
POST /search/subreddits?query=building%20b2b%20saas&limit=10
```

**Response**:
```json
{
  "query": "building b2b saas",
  "count": 10,
  "filters": {
    "min_subscribers": 1000,
    "exclude_nsfw": true
  },
  "results": [
    {
      "subreddit_name": "SaaS",
      "title": "Software As a Service Companies...",
      "public_description": "Discussions and useful links...",
      "subscribers": 459668,
      "score": 0.857
    }
  ]
}
```

#### **Discover Subreddits**
```bash
POST /discover/subreddits?query=saas&limit=50
```

Searches Reddit, scrapes metadata, and stores in database.

#### **Embedding Statistics**
```bash
GET /embeddings/stats
```

Shows embedding coverage and model information.

### **Database Schema**

**Collection**: `subreddit_discovery`

```javascript
{
  // Identifiers
  "subreddit_name": "SaaS",
  "display_name": "SaaS",

  // Text fields (for embeddings)
  "title": "Software As a Service...",
  "public_description": "Discussions and useful links...",
  "description": "Full markdown description...",
  "guidelines_text": "Posting guidelines...",
  "rules_text": "Rule 1: ... | Rule 2: ...",
  "sample_posts_titles": "Title 1 | Title 2 | ...",

  // Structured data
  "rules": [
    {"short_name": "...", "description": "..."},
    // ... more rules
  ],
  "sample_posts": [
    {"title": "...", "score": 604, "num_comments": 234},
    // ... top 20 posts
  ],

  // Metadata (for filtering)
  "subscribers": 459668,
  "active_user_count": 1234,
  "subreddit_type": "public",
  "over_18": false,
  "lang": "en",
  "advertiser_category": "Business / Finance",

  // Embeddings (1536 dimensions)
  "embeddings": {
    "combined_embedding": [0.123, -0.456, ...],  // 1536 floats
    "model": "text-embedding-3-small",
    "dimensions": 1536,
    "generated_at": ISODate("2025-11-23...")
  }
}
```

### **How It Works**

1. **Discovery**: Search Reddit for subreddits matching topics
2. **Metadata Collection**: Scrape comprehensive data (rules, guidelines, sample posts)
3. **Embedding Generation**: Combine all text fields into rich semantic representation
4. **Vector Indexing**: Create MongoDB Atlas vector search index (HNSW algorithm)
5. **Semantic Search**: Query embeddings using natural language, rank by cosine similarity

### **Embedding Model Details**

**Azure OpenAI text-embedding-3-small**:
- **Dimensions**: 1536
- **Context Window**: 8,191 tokens
- **MTEB Score**: 62.3% (good accuracy, optimized for speed/cost)
- **Performance**: Very fast API calls (~100+ embeddings/second)
- **Cost**: ~$0.00002 per 1K tokens
- **Advantage**: Lightweight deployment, no local GPU/CPU inference needed

### **Example Queries**

| Query | Top Results |
|-------|-------------|
| "building b2b saas" | r/SaaS, r/startups, r/Entrepreneur, r/B2B |
| "cryptocurrency trading strategies" | r/CryptoCurrency, r/CryptoMarkets, r/BitcoinMarkets |
| "indie game development tips" | r/gamedev, r/IndieDev, r/Unity3D |
| "machine learning projects" | r/MachineLearning, r/learnmachinelearning, r/datascience |
| "stock market investing advice" | r/stocks, r/investing, r/wallstreetbets |

### **Cost Analysis**

**One-Time Setup**:
- No model download required
- Minimal disk space (~200MB Docker image)

**Ongoing Costs**:
- **Embedding generation**: ~$0.00002 per 1K tokens
- **Storage**: ~6 KB per subreddit (1536 floats × 4 bytes)
- **For 1000 subreddits × 2K tokens**: ~$0.04 (one-time)
- **Total**: Very low cost (~$0.04 per 1000 subreddits)

**Benefits vs Local Models**:
- No GPU/large CPU required
- Docker image: ~200MB (vs ~1.5GB with PyTorch)
- Faster deployment and scaling
- No model download/update management

### **Performance Expectations**

**Embedding Generation** (Azure OpenAI API):
- 10 subreddits: ~5 seconds
- 100 subreddits: ~30 seconds
- 1000 subreddits: ~5 minutes

**Search Latency**:
- Query embedding: ~50-100ms (API call)
- Vector search (10K docs): <100ms
- **Total**: <200ms per query

**Accuracy**:
- MTEB score: 62.3%
- High-quality embeddings for semantic search
- Captures semantic meaning effectively

### **Troubleshooting**

**"Failed to connect to Azure OpenAI"**:
```bash
# Check environment variables are set
echo $AZURE_OPENAI_ENDPOINT
echo $AZURE_OPENAI_API_KEY
# Verify deployment name (optional, defaults to text-embedding-3-small)
echo $AZURE_EMBEDDING_DEPLOYMENT
```

**"Vector search failed"**:
- Ensure MongoDB Atlas (not self-hosted MongoDB)
- Create vector index: `python discovery/setup_vector_index.py`
- Verify embeddings exist: `python discovery/generate_embeddings.py --stats`

**"No results found"**:
- Relax filters: `--min-subscribers 0`
- Discover more subreddits: `python discovery/discover_subreddits.py`
- Check embeddings: `python discovery/generate_embeddings.py --stats`

### **Configuration**

**config.py** settings:

```python
EMBEDDING_CONFIG = {
    "model_name": "text-embedding-3-small",
    "dimensions": 1536,
    "context_window": 8191,
    "batch_size": 32,
    "similarity_metric": "cosine"
}

DISCOVERY_CONFIG = {
    "collection_name": "subreddit_discovery",
    "vector_index_name": "subreddit_vector_index",
    "default_search_limit": 10,
    "default_min_subscribers": 1000,
    "sample_posts_limit": 20
}

EMBEDDING_WORKER_CONFIG = {
    "enabled": True,
    "check_interval": 60,           # Seconds between checks
    "batch_size": 10,               # Max subreddits per batch
    "metadata_vector_index_name": "metadata_vector_index"
}
```

### **Files**

| File | Purpose |
|------|---------|
| `discovery/discover_subreddits.py` | Search Reddit and scrape subreddit metadata |
| `discovery/generate_embeddings.py` | Generate semantic embeddings (combined + persona) |
| `discovery/llm_enrichment.py` | LLM-based audience profiling via Azure GPT-4o-mini |
| `discovery/enrich_existing.py` | Batch enrich existing subreddits with audience data |
| `discovery/test_persona_search.py` | Test and compare persona-based search |
| `discovery/setup_vector_index.py` | Create MongoDB vector search indexes |
| `discovery/semantic_search.py` | CLI semantic search tool |
| `embedding_worker.py` | Background worker: embeddings + LLM enrichment pipeline |
| `tools/repair_ghost_posts.py` | Data integrity repair utility |
| `api.py` | REST API endpoints for search & discovery |
| `config.py` | Embedding, enrichment, and discovery configuration |

### **Automatic Embeddings & Enrichment for Active Scrapers (v1.5+)**

Subreddits being actively scraped automatically get full persona search capability through a 3-step pipeline.

**How It Works:**
1. When `posts_scraper.py` saves subreddit metadata, it sets `embedding_status: "pending"`
2. Background worker processes pending subreddits every 60 seconds with a 3-step pipeline:
   - **Step 1**: Generate combined embedding (topic-focused)
   - **Step 2**: Run LLM enrichment via Azure GPT-4o-mini (audience profile)
   - **Step 3**: Generate persona embedding (audience-focused)
3. Each step checks if data already exists - **no duplicate processing**
4. New subreddits get full persona search capability automatically

**Data Flow:**
```
Scraper Container              API Server                    MongoDB
┌─────────────────┐          ┌───────────────────┐         ┌──────────────────┐
│ posts_scraper   │          │ Background Worker │         │ subreddit_       │
│                 │          │ (every 60s)       │         │ metadata         │
│ Saves metadata  │─────────>│                   │         │                  │
│ + sets:         │          │ 3-Step Pipeline:  │<────────│ embedding_status │
│ embedding_      │          │ 1. Combined emb   │         │ : "pending"      │
│ status:pending  │          │ 2. LLM enrichment │────────>│                  │
└─────────────────┘          │ 3. Persona emb    │         │ embeddings.*     │
                             └───────────────────┘         │ llm_enrichment   │
                                                           └──────────────────┘
```

**Deduplication:**
- Each step skips if data already exists
- LLM enrichment runs **once per subreddit** (no repeated API costs)
- Re-processing only happens if content changes significantly

**API Endpoints:**
- `GET /embeddings/worker/status` - Worker status and statistics
- `POST /embeddings/worker/process` - Manually trigger processing
- `POST /embeddings/worker/process?subreddit=X` - Process specific subreddit

**CLI Commands:**
```bash
# Check worker stats
python embedding_worker.py --stats

# Process all pending
python embedding_worker.py --process-all

# Process specific subreddit
python embedding_worker.py --subreddit wallstreetbets

# Reset failed embeddings
python embedding_worker.py --reset-failed
```

**Setup Vector Indexes:**
```bash
# Create combined embedding index
python discovery/setup_vector_index.py --collection metadata

# Create persona embedding index
python discovery/setup_vector_index.py --collection metadata --embedding-type persona

# Create both indexes
python discovery/setup_vector_index.py --collection metadata --embedding-type all
```

**Database Schema (subreddit_metadata):**
```javascript
{
  // ... existing fields ...

  // Embedding tracking
  "embedding_status": "pending" | "complete" | "failed",
  "embedding_requested_at": ISODate("..."),

  // LLM Enrichment (audience profile)
  "llm_enrichment": {
    "audience_profile": "Startup founders and indie hackers...",
    "audience_types": ["SaaS founders", "indie hackers", ...],
    "user_intents": ["validate ideas", "find customers", ...],
    "pain_points": ["customer acquisition", "pricing", ...],
    "content_themes": ["product launches", "growth hacks", ...]
  },
  "llm_enrichment_at": ISODate("..."),

  // Embeddings
  "embeddings": {
    "combined_embedding": [/* 1536 floats - topic focused */],
    "persona_embedding": [/* 1536 floats - audience focused */],
    "model": "text-embedding-3-small",
    "dimensions": 1536,
    "generated_at": ISODate("..."),
    "persona_generated_at": ISODate("...")
  }
}
```

**Required Environment Variables:**
```bash
# For embeddings and LLM enrichment (Azure OpenAI)
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_EMBEDDING_DEPLOYMENT=text-embedding-3-small  # Optional, defaults to text-embedding-3-small
AZURE_DEPLOYMENT_NAME=gpt-4o-mini  # Optional, defaults to gpt-4o-mini
```
