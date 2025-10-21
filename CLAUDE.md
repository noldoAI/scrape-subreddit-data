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
