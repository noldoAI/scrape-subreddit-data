# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Reddit scraping system** with two deployment modes:
1. **API Management Mode** (Primary): Web dashboard + REST API for managing multiple containerized scrapers
2. **Standalone Mode**: Direct Python script execution for single-subreddit scraping

**Key Architecture**: Each subreddit scraper runs in its own Docker container with isolated Reddit API credentials to avoid rate limit conflicts. All scrapers share a MongoDB database.

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
docker ps --filter "name=reddit-scraper-"

# View logs from a specific scraper container
docker logs reddit-scraper-wallstreetbets

# Stop all scraper containers
docker stop $(docker ps --filter "name=reddit-scraper-" -q)
```

### Standalone Mode (Alternative)

```bash
# Install dependencies
pip install -r requirements.txt

# Run scraper for a specific subreddit
python reddit_scraper.py SUBREDDIT_NAME --posts-limit 1000 --interval 300 --comment-batch 20

# Show statistics only
python reddit_scraper.py SUBREDDIT_NAME --stats

# Run comment scraping only
python reddit_scraper.py SUBREDDIT_NAME --comments-only

# Update subreddit metadata only
python reddit_scraper.py SUBREDDIT_NAME --metadata-only

# Using Docker Compose with standalone mode
docker-compose --env-file .env.wallstreetbets up -d
docker-compose logs -f
docker-compose down
```

## Code Architecture

### Core Components

1. **`api.py`** - FastAPI management server
   - Creates/manages Docker containers for each subreddit scraper
   - Stores scraper configurations and encrypted credentials in MongoDB (`reddit_scrapers` collection)
   - Provides REST API endpoints and web dashboard
   - Monitors container health and auto-restarts failed scrapers
   - Each API endpoint manages containers via Docker commands

2. **`reddit_scraper.py`** - Unified scraping engine (runs in containers)
   - Single Python process handles all three scraping phases in a continuous loop:
     - **Phase 1**: Posts scraping (every N seconds based on config)
     - **Phase 2**: Smart comment updates (prioritized queue system)
     - **Phase 3**: Subreddit metadata (every 24 hours)
   - Uses PRAW library to interact with Reddit API
   - Implements intelligent comment update prioritization

3. **`config.py`** - Centralized configuration
   - Database name: `"noldo"`
   - Collection names: `reddit_posts`, `reddit_comments`, `subreddit_metadata`, `reddit_scrapers`, `reddit_accounts`
   - Default scraper settings (interval, limits, batch sizes)
   - Docker, API, monitoring, and security configurations

4. **`rate_limits.py`** - Reddit API rate limiting
   - Monitors Reddit API quota (remaining/used requests)
   - Automatically pauses when rate limit is low
   - Waits for rate limit reset when necessary

### Data Flow Architecture

```
Web Dashboard/API Request
         ↓
    api.py creates Docker container
         ↓
Container runs reddit_scraper.py with unique credentials
         ↓
    Continuous 3-phase loop:
    1. Scrape hot posts → Update MongoDB (reddit_posts)
    2. Smart comment updates → Update MongoDB (reddit_comments)
    3. Metadata scraping → Update MongoDB (subreddit_metadata)
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
3. **Complete Comment Pagination**: `replace_more(limit=None)` expands ALL nested comment threads (was limited to 10)
4. **Retry Logic**: Automatic retry with exponential backoff for transient failures
5. **Repair Script**: `repair_ghost_posts.py` identifies and fixes existing corrupted data

**Configuration Options** (in config.py):
- `replace_more_limit`: `None` = expand all comments, or set integer limit (default: `None`)
- `max_retries`: Number of retry attempts for failed operations (default: `3`)
- `retry_backoff_factor`: Exponential backoff multiplier (default: `2` = 2s, 4s, 8s)
- `verify_before_marking`: Enable verification step before marking posts scraped (default: `True`)

**Repair Utility** ([repair_ghost_posts.py](repair_ghost_posts.py)):
```bash
# Show statistics about data integrity issues
python repair_ghost_posts.py --stats-only

# Show what would be repaired (dry run)
python repair_ghost_posts.py --dry-run

# Actually repair ghost posts
python repair_ghost_posts.py

# Repair specific subreddit
python repair_ghost_posts.py --subreddit wallstreetbets

# Also repair incomplete posts (missing >10% of comments)
python repair_ghost_posts.py --include-incomplete
```

### Smart Comment Update Prioritization

The system uses a priority queue for comment updates:
1. **HIGHEST**: Posts never scraped (initial scrape)
2. **HIGH**: Recent posts (<24h) - update every 6 hours
3. **MEDIUM**: Older posts - update every 24 hours
4. **Deduplication**: Only collects NEW comments, skips existing ones

Query uses `$or` conditions with `initial_comments_scraped`, `last_comment_fetch_time`, and `created_datetime` to determine priority.

### Container Isolation Strategy

- Each subreddit runs in its own Docker container (named `reddit-scraper-{subreddit}`)
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
- **Container restart required**: Changes to reddit_scraper.py require rebuilding Docker image and restarting containers
- **API changes**: If modifying API endpoints, update both the endpoint handler and the HTML dashboard in api.py

### Rate Limiting Best Practices

- Always call `check_rate_limit(reddit)` before making Reddit API calls
- Current threshold: pauses when <50 requests remaining
- Add `time.sleep(2)` between comment scraping to be respectful

### Docker Container Lifecycle

1. API receives start request with credentials
2. Credentials encrypted and stored in MongoDB
3. Container created with environment variables (decrypted credentials)
4. Container runs `reddit_scraper.py` with command-line args
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
python reddit_scraper.py wallstreetbets --stats
python reddit_scraper.py wallstreetbets --posts-limit 100 --interval 60 --comment-batch 5
```

### Debugging Container Issues

```bash
# Check if container is running
docker ps -a --filter "name=reddit-scraper-wallstreetbets"

# View container logs
docker logs reddit-scraper-wallstreetbets --tail 100

# Inspect container details
docker inspect reddit-scraper-wallstreetbets

# Enter running container for debugging
docker exec -it reddit-scraper-wallstreetbets bash

# Check resource usage
docker stats reddit-scraper-wallstreetbets
```

### Common Issues

**Container exits immediately**:
- Check logs: `docker logs reddit-scraper-{subreddit}`
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
- **Solution**: Run `python repair_ghost_posts.py` to identify and fix affected posts
- **Prevention**: Ensure `verify_before_marking: True` in config (default in v1.1+)

**Incomplete comment data (missing comments)**:
- **Symptom**: Post has fewer comments in DB than `num_comments` field indicates
- **Cause**: Earlier versions used `replace_more(limit=10)` which missed deeply nested comments
- **Solution**: Run `python repair_ghost_posts.py --include-incomplete` to reset affected posts
- **Prevention**: v1.1+ uses `replace_more(limit=None)` to capture ALL comments

**Verification failures in logs**:
- **Symptom**: Logs show "VERIFICATION FAILED - 0 comments in DB"
- **Cause**: Comments failed to save to database (network issue, MongoDB problem)
- **Action**: Check `reddit_scrape_errors` collection for details
- **Resolution**: Failed posts will automatically retry on next cycle

## API Endpoints Reference

### Scraper Management
- `POST /scrapers/start` - Start new scraper container
- `GET /scrapers` - List all scrapers
- `GET /scrapers/{subreddit}/status` - Container status
- `POST /scrapers/{subreddit}/stop` - Stop container
- `POST /scrapers/{subreddit}/restart` - Restart container
- `DELETE /scrapers/{subreddit}` - Remove scraper and config
- `GET /scrapers/{subreddit}/stats` - Scraping statistics
- `GET /scrapers/{subreddit}/logs?lines=100` - Container logs

### System Monitoring
- `GET /health` - System health (database + Docker status)
- `GET /presets` - Configuration presets for different subreddit types
- `POST /scrapers/restart-all-failed` - Restart all failed containers

## Configuration Presets

The system includes presets for different subreddit activity levels:

- **High Activity** (wallstreetbets, stocks): posts_limit=2000, interval=180s, comment_batch=30
- **Medium Activity** (investing, crypto): posts_limit=1000, interval=300s, comment_batch=20
- **Low Activity** (pennystocks, niche): posts_limit=500, interval=600s, comment_batch=10

These are defined in api.py and accessible via `GET /presets` endpoint.
