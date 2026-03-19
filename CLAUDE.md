# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Reddit scraping system** with two deployment modes:
1. **API Management Mode** (Primary): Web dashboard + REST API for managing multiple containerized scrapers
2. **Standalone Mode**: Direct Python script execution for single-subreddit scraping

**Key Architecture**: Each subreddit scraper runs in its own Docker container with isolated Reddit API credentials to avoid rate limit conflicts. All scrapers share a MongoDB database (database name: `"noldo"`).

## Project Structure

```
scrape-subreddit-data/
├── api.py                    # FastAPI management server (main entry point)
├── posts_scraper.py          # Posts scraping engine (runs in Docker containers)
├── comments_scraper.py       # Comments scraping engine (runs in Docker containers)
├── config.py                 # Centralized configuration
├── embedding_worker.py       # Background embedding worker
├── core/                     # Core shared utilities
│   ├── rate_limits.py        # Reddit API rate limiting utility
│   ├── azure_logging.py      # Azure Application Insights logging
│   └── metrics.py            # Prometheus metrics
├── tracking/                 # API usage tracking & cost calculation
│   ├── http_request_counter.py   # HTTP request counting at transport layer
│   └── api_usage_tracker.py      # MongoDB storage for usage stats + cost
├── api/                      # API module helpers
│   ├── models.py             # Pydantic models
│   └── services/encryption.py   # Credential encryption
├── discovery/                # Semantic search & subreddit discovery
│   ├── discover_subreddits.py    # Search Reddit for subreddits
│   ├── generate_embeddings.py    # Generate semantic embeddings
│   ├── llm_enrichment.py         # LLM-based audience profiling
│   ├── enrich_existing.py        # Batch enrich existing subreddits
│   ├── semantic_search.py        # CLI semantic search tool
│   └── setup_vector_index.py     # MongoDB vector index setup
├── tools/repair_ghost_posts.py   # Data integrity repair
├── tests/                    # Test modules
├── docs/                     # Documentation (API setup, pricing, servers)
├── Dockerfile               # Scraper container image
├── Dockerfile.api           # API server container image
└── docker-compose*.yml      # Container orchestration
```

## Common Commands

### API Management Mode
```bash
docker build -f Dockerfile -t reddit-scraper .          # Build scraper image
docker-compose -f docker-compose.api.yml up -d          # Start API server
docker-compose -f docker-compose.api.yml logs -f        # View API logs
docker-compose -f docker-compose.api.yml down            # Stop API server
# Dashboard: http://localhost:8000
```

### Standalone Mode
```bash
pip install -r requirements.txt
python posts_scraper.py SUBREDDIT --posts-limit 1000 --interval 300
python comments_scraper.py SUBREDDIT --interval 300 --comment-batch 12
python posts_scraper.py SUBREDDIT --stats                # Stats only
python posts_scraper.py stocks,investing,wallstreetbets --posts-limit 50  # Multi-sub
```

## Core Components

1. **`api.py`** - FastAPI management server: creates/manages Docker containers, stores encrypted credentials in MongoDB (`reddit_scrapers` collection), monitors health, auto-restarts failed scrapers. Supports `scraper_type`: "posts" or "comments".

2. **`posts_scraper.py`** - Posts scraping engine: continuous loop with Phase 1 (posts via multi-sort: new, top, rising) and Phase 2 (subreddit metadata every 24h). Uses PRAW. First-run fetches month of historical top posts, then switches to daily.

3. **`comments_scraper.py`** - Comments scraping engine: priority-based comment scraping (HIGHEST: never scraped, HIGH: >100 comments/2h, MEDIUM: 20-100/6h, LOW: <20/24h). Depth-limited to top 3 levels. Deduplication avoids re-scraping.

4. **`config.py`** - Centralized configuration. Collections: `reddit_posts`, `reddit_comments`, `subreddit_metadata`, `reddit_scrapers`, `reddit_accounts`, `reddit_scrape_errors`. Contains `DEFAULT_POSTS_SCRAPER_CONFIG`, `DEFAULT_COMMENTS_SCRAPER_CONFIG`, `MULTI_SCRAPER_CONFIG`, `EMBEDDING_CONFIG`, `DISCOVERY_CONFIG`, `API_USAGE_CONFIG`.

5. **`core/`** - `rate_limits.py`: monitors Reddit API quota, auto-pauses when <50 remaining. `azure_logging.py`: Azure App Insights (WARNING+ only). `metrics.py`: Prometheus metrics.

6. **`tracking/`** - `CountingSession` wraps `requests.Session` to count actual HTTP requests at transport layer (PRAW makes many more HTTP requests than API calls visible in code). `APIUsageTracker` stores usage/cost in MongoDB. Cost: $0.24 per 1,000 requests.

## MongoDB Collections

| Collection | Key Fields | Indexes |
|-----------|-----------|---------|
| `reddit_posts` | `post_id` (unique), title, score, author, `comments_scraped`, `initial_comments_scraped`, `last_comment_fetch_time` | `post_id` |
| `reddit_comments` | `comment_id` (unique), `post_id`, `parent_id`, `depth`, body, score | `comment_id`, `post_id`, `parent_id` |
| `subreddit_metadata` | `subreddit_name` (unique), subscribers, rules, `last_updated`, `embedding_status`, `embeddings`, `llm_enrichment` | `subreddit_name` |
| `reddit_scrapers` | `subreddit` (unique), `container_id`, status, config, credentials (encrypted), `auto_restart`, `pending_scrape`, `scrape_failures` | `subreddit` |
| `reddit_accounts` | Reddit account credentials for reuse across scrapers | |
| `reddit_scrape_errors` | subreddit, `post_id`, `error_type`, `error_message`, `retry_count`, resolved | |
| `reddit_api_usage` | subreddit, `scraper_type`, `actual_http_requests`, `estimated_cost_usd`, timestamp | |
| `subreddit_discovery` | `subreddit_name`, metadata, rules, sample_posts, `embeddings.combined_embedding` (1536d) | |

## Multi-Subreddit Mode & Dynamic Queue

Scrape unlimited subreddits with 1 Reddit account in a single container using rotation. System self-throttles via Reddit's rate limit API.

**Key behaviors:**
- 1 container handles unlimited subreddits in rotation with 2s delay between subs
- Dynamic queue: add/remove subreddits via API without container restart (changes picked up within 30-60s)
- `pending_scrape` array tracks new subreddits awaiting first scrape — processed with priority
- After 3 consecutive failures, subreddit removed from `pending_scrape`
- Re-adding a failed subreddit resets its failure counter
- MongoDB queue takes precedence over CLI args; falls back to CLI if DB read fails

**API endpoints for queue management:**
- `POST /scrapers/{id}/subreddits/add` — add subreddits (deduplicated)
- `POST /scrapers/{id}/subreddits/remove` — remove subreddits (protects primary)
- `PATCH /scrapers/{id}/subreddits` — replace entire list

**Starting multi-sub scraper:**
```bash
POST /scrapers/start-flexible
{ "subreddits": ["stocks", "investing", "wallstreetbets"], "scraper_type": "posts",
  "posts_limit": 50, "interval": 300, "saved_account_name": "my_account" }
```

## Data Integrity

- **Verification before marking**: Comments verified in DB before setting `comments_scraped: True`
- **Error logging**: Failed scrapes logged to `reddit_scrape_errors`, NOT marked as complete
- **Depth-limited scraping**: Top 3 levels captures 85-90% of valuable discussion
- **Retry logic**: Exponential backoff for transient failures
- **Repair script**: `python tools/repair_ghost_posts.py` (use `--stats-only`, `--dry-run`, or `--subreddit X`)

## Container & Security

- Each subreddit gets containers: `reddit-posts-scraper-{subreddit}` and `reddit-comments-scraper-{subreddit}`
- Credentials: Fernet encryption, key at `/tmp/.scraper_key`, encrypted in MongoDB, decrypted when launching containers, masked as `"***"` in API responses
- API server mounts Docker socket (`/var/run/docker.sock`)
- Health check every 30s, auto-restart with cooldown if `auto_restart=True`

## Environment Variables

```bash
# Required (.env file)
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/dbname
R_CLIENT_ID=...  R_CLIENT_SECRET=...  R_USERNAME=...  R_PASSWORD=...  R_USER_AGENT=...

# For embeddings/LLM enrichment (Azure OpenAI)
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_EMBEDDING_DEPLOYMENT=text-embedding-3-small   # optional, default
AZURE_DEPLOYMENT_NAME=gpt-4o-mini                    # optional, default

# For Azure logging (optional)
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...
```

## Important Implementation Details

### When Modifying Comment Scraping Logic
- **Preserve tracking fields**: When updating posts in `save_posts_to_db()`, existing `comments_scraped`, `initial_comments_scraped`, `last_comment_fetch_time` MUST be preserved
- **Update tracking after scraping**: Always call `mark_posts_comments_updated()` after success
- **Deduplication**: Use `get_existing_comment_ids()` to avoid re-scraping
- **Verify before marking**: Only mark posts as scraped AFTER verifying comments are in DB
- **Error logging**: Log failures to `reddit_scrape_errors` collection

### When Adding New Scraper Features
- Update `config.py` first with new configuration options
- Container restart required for changes to `posts_scraper.py` or `comments_scraper.py`
- If modifying API endpoints, update both the handler and HTML dashboard in `api.py`

### Rate Limiting
- Always call `check_rate_limit(reddit)` before each subreddit in multi-sub mode
- Pauses when <50 requests remaining, sleeps until quota reset
- Add `time.sleep(2)` between subreddits for politeness
- No artificial subreddit limits — system self-throttles

### Key Configuration (config.py)
- `replace_more_limit`: `0` = skip MoreComments (fastest), `None` = expand all
- `max_comment_depth`: `3` = top 4 levels (0-indexed)
- `posts_per_comment_batch`: `12` posts per cycle
- `sorting_methods`: `["new", "top", "rising"]`
- `top_time_filter`: `"day"` (regular), `initial_top_time_filter`: `"month"` (first run)
- `max_retries`: `3`, `retry_backoff_factor`: `2`
- `verify_before_marking`: `True`

## API Endpoints Reference

### Scraper Management
- `POST /scrapers/start` — Start single scraper
- `POST /scrapers/start-flexible` — Start multi-subreddit scraper
- `GET /scrapers` — List all scrapers (includes DB totals + metrics)
- `GET /scrapers/{subreddit}/status` — Container status
- `POST /scrapers/{subreddit}/stop` — Stop container
- `POST /scrapers/{subreddit}/restart` — Restart container
- `DELETE /scrapers/{subreddit}` — Remove scraper and config
- `GET /scrapers/{subreddit}/logs?lines=100` — Container logs
- `GET /scrapers/{subreddit}/stats` — Subreddit stats (`?detailed=true` for top posts/authors)
- `GET /stats/global` — Cross-subreddit statistics

### Queue Management
- `POST /scrapers/{id}/subreddits/add` — Add to queue
- `POST /scrapers/{id}/subreddits/remove` — Remove from queue
- `PATCH /scrapers/{id}/subreddits` — Replace queue

### Search & Discovery
- `POST /search/subreddits?query=...&limit=10` — Semantic search
- `POST /discover/subreddits?query=...&limit=50` — Discover and scrape metadata
- `GET /embeddings/stats` — Embedding coverage

### System
- `GET /health` — System health
- `GET /presets` — Configuration presets
- `POST /scrapers/restart-all-failed` — Restart all failed containers
- `GET /metrics` — Prometheus metrics
- `GET /api/usage/cost` — Cost statistics (`?subreddit=X` for specific)

### Embedding Worker
- `GET /embeddings/worker/status` — Worker status
- `POST /embeddings/worker/process` — Trigger processing (`?subreddit=X` for specific)

## Monitoring

### Prometheus + Grafana
```bash
docker-compose -f docker-compose.monitoring.yml up -d
# Grafana: http://localhost:3000 (admin/admin) | Prometheus: http://localhost:9090
```

Key metrics: `reddit_scraper_posts_total`, `reddit_scraper_comments_total`, `reddit_scraper_status` (1=running, 0=stopped, -1=failed), `reddit_scraper_posts_per_hour`, `reddit_scraper_errors_unresolved`, `reddit_scraper_up`, `reddit_database_connected`.

Config files: `monitoring/prometheus.yml`, `monitoring/alerts.yml`, `monitoring/alertmanager.yml`, `monitoring/grafana/dashboards/`.

### Azure Application Insights
WARNING+ logs sent to Azure (INFO stays local). Set `APPLICATIONINSIGHTS_CONNECTION_STRING` in `.env`. Gracefully degrades if not configured.

## Semantic Subreddit Search

Search by meaning: `"building b2b saas"` → finds r/SaaS, r/startups, r/Entrepreneur.

**Stack**: Azure OpenAI `text-embedding-3-small` (1536d) + MongoDB Atlas Vector Search (HNSW).

**Pipeline:**
1. `discovery/discover_subreddits.py` — Search Reddit, scrape metadata (rules, guidelines, sample posts)
2. `discovery/generate_embeddings.py` — Generate embeddings
3. `discovery/setup_vector_index.py` — Create vector index (one-time)
4. `discovery/semantic_search.py` — CLI search tool

**Automatic pipeline for active scrapers**: When `posts_scraper.py` saves metadata, it sets `embedding_status: "pending"`. Background worker (`embedding_worker.py`) processes pending subreddits every 60s: (1) combined embedding, (2) LLM enrichment via GPT-4o-mini, (3) persona embedding. Each step skips if data exists.

```bash
python embedding_worker.py --stats              # Check worker stats
python embedding_worker.py --process-all         # Process all pending
python embedding_worker.py --subreddit X         # Process specific
python discovery/setup_vector_index.py --collection metadata --embedding-type all  # Create indexes
```

## API Cost Tracking

Reddit API costs $0.24 per 1,000 HTTP requests. `CountingSession` counts actual HTTP requests at transport layer (PRAW undercounts by 2-5x). See `docs/REDDIT_API_PRICING.md` for details.

Dashboard shows: today's cost, last hour, avg/hour, avg/day, monthly projection. Auto-refreshes every 60s.

## Azure VM Deployment

```bash
# SSH to production VM
ssh -i ~/.ssh/noldo-data-server-key.pem azureuser@20.64.246.60
# Or with SSH config alias: ssh noldo-azure

# VM management
az vm show --resource-group noldo-data-server --name noldo-data-server --query "powerState"
az vm start --resource-group noldo-data-server --name noldo-data-server
az vm stop --resource-group noldo-data-server --name noldo-data-server
```

## Debugging

```bash
docker ps -a --filter "name=reddit-posts-scraper-"      # List containers
docker logs reddit-posts-scraper-{subreddit} --tail 100  # View logs
docker inspect reddit-posts-scraper-{subreddit}          # Inspect
docker exec -it reddit-posts-scraper-{subreddit} bash    # Enter container
```

**Common issues**: Container exits immediately → check logs, verify credentials/MongoDB URI, ensure image exists. Rate limits → use unique credentials per scraper. Ghost posts → run `repair_ghost_posts.py`. Verification failures → check `reddit_scrape_errors` collection.
