# Reddit Scraper

A comprehensive Reddit scraping system that continuously collects posts, comments, and subreddit metadata for any specified subreddit using one set of Reddit API credentials.

## Features

- - System\*\*: One script handles posts, comments, and subreddit metadata
- **Configurable**: Target any subreddit by name
- **Continuous Updates**: Live comment tracking with smart deduplication
- **Bulk Operations**: High-performance MongoDB operations
- **Rate Limiting**: Automatic Reddit API rate limit handling
- **Comment Trees**: Preserves hierarchical comment structure
- **Smart Scheduling**: Prioritized updates based on post age and activity
- **Docker Ready**: Easy deployment with customizable parameters

## Quick Reference

**Most Common Usage Patterns:**

```bash
# Default settings (good for most subreddits)
python reddit_scraper.py SUBREDDIT_NAME

# High-activity subreddit (wallstreetbets, stocks)
python reddit_scraper.py wallstreetbets --posts-limit 2000 --interval 180 --comment-batch 30

# Medium subreddit (investing, cryptocurrency)
python reddit_scraper.py investing --posts-limit 1000 --interval 300 --comment-batch 20

# Small/niche subreddit (pennystocks, specific trading)
python reddit_scraper.py pennystocks --posts-limit 500 --interval 600 --comment-batch 10

# Just check current statistics
python reddit_scraper.py SUBREDDIT_NAME --stats

# Docker with environment variables
TARGET_SUBREDDIT=wallstreetbets POSTS_LIMIT=2000 SCRAPE_INTERVAL=180 docker-compose up
```

**Required Variables:**

- `SUBREDDIT_NAME`: The target subreddit (without r/)
- Plus optional configuration: `--posts-limit`, `--interval`, `--comment-batch`

## Predefined Configurations

**Copy-paste ready commands for popular subreddit types:**

### High-Volume Financial Subreddits

```bash
# WallStreetBets (very active, needs high throughput)
python reddit_scraper.py wallstreetbets --posts-limit 2000 --interval 180 --comment-batch 30

# Stocks (high activity)
python reddit_scraper.py stocks --posts-limit 1500 --interval 240 --comment-batch 25

# Cryptocurrency (high activity, fast-moving)
python reddit_scraper.py cryptocurrency --posts-limit 1500 --interval 200 --comment-batch 25
```

### Medium-Volume Investment Subreddits

```bash
# Investing (steady activity)
python reddit_scraper.py investing --posts-limit 1000 --interval 300 --comment-batch 20

# SecurityAnalysis (moderate activity)
python reddit_scraper.py securityanalysis --posts-limit 800 --interval 400 --comment-batch 15

# ValueInvesting (moderate activity)
python reddit_scraper.py valueinvesting --posts-limit 600 --interval 450 --comment-batch 12
```

### Specialized/Smaller Trading Subreddits

```bash
# PennyStocks (smaller, less frequent)
python reddit_scraper.py pennystocks --posts-limit 500 --interval 600 --comment-batch 10

# Options trading (focused discussions)
python reddit_scraper.py options --posts-limit 400 --interval 500 --comment-batch 8

# DayTrading (time-sensitive but smaller volume)
python reddit_scraper.py daytrading --posts-limit 600 --interval 300 --comment-batch 12
```

### Docker Environment Files

**Create `.env.wallstreetbets`:**

```bash
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=2000
SCRAPE_INTERVAL=180
COMMENT_BATCH=30
# Add your Reddit API credentials here
R_CLIENT_ID=your_client_id
R_CLIENT_SECRET=your_secret
R_USERNAME=your_username
R_PASSWORD=your_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/dbname
```

**Create `.env.investing`:**

```bash
TARGET_SUBREDDIT=investing
POSTS_LIMIT=1000
SCRAPE_INTERVAL=300
COMMENT_BATCH=20
# Add your Reddit API credentials here
R_CLIENT_ID=your_client_id
R_CLIENT_SECRET=your_secret
R_USERNAME=your_username
R_PASSWORD=your_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/dbname
```

Then run with:

```bash
docker-compose -f docker-compose.yml --env-file .env.wallstreetbets up -d
# or
docker-compose -f docker-compose.yml --env-file .env.investing up -d
```

## Quick Start

### 1. Set up environment variables

Create a `.env` file with your Reddit API credentials and MongoDB Atlas connection:

```bash
# Reddit API Credentials (get from https://www.reddit.com/prefs/apps)
R_CLIENT_ID=your_reddit_client_id
R_CLIENT_SECRET=your_reddit_client_secret
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername

# MongoDB Atlas Connection
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/dbname?retryWrites=true&w=majority
```

### 2. Run the scraper with configuration

```bash
# Basic usage - scrape any subreddit with default settings
python reddit_scraper.py wallstreetbets
python reddit_scraper.py stocks
python reddit_scraper.py investing

# Configure scraping parameters for specific needs
python reddit_scraper.py wallstreetbets --posts-limit 500 --interval 600 --comment-batch 10
python reddit_scraper.py cryptocurrency --posts-limit 2000 --interval 180 --comment-batch 30
python reddit_scraper.py pennystocks --posts-limit 300 --interval 900 --comment-batch 5

# Show statistics only
python reddit_scraper.py wallstreetbets --stats

# Run specific components only
python reddit_scraper.py wallstreetbets --comments-only
python reddit_scraper.py wallstreetbets --metadata-only
```

### 3. Configuration Parameters

When running the scraper, you can customize these parameters:

| Parameter         | Default  | Description                             | Example              |
| ----------------- | -------- | --------------------------------------- | -------------------- |
| `subreddit`       | required | Target subreddit name (without r/)      | `wallstreetbets`     |
| `--posts-limit`   | 1000     | Hot posts to fetch per cycle            | `--posts-limit 500`  |
| `--interval`      | 300      | Seconds between scrape cycles           | `--interval 600`     |
| `--comment-batch` | 20       | Posts to process for comments per cycle | `--comment-batch 10` |

**Examples for different subreddit sizes:**

```bash
# Large active subreddit (high volume)
python reddit_scraper.py wallstreetbets --posts-limit 2000 --interval 180 --comment-batch 30

# Medium subreddit (moderate activity)
python reddit_scraper.py investing --posts-limit 1000 --interval 300 --comment-batch 20

# Small subreddit (low activity)
python reddit_scraper.py pennystocks --posts-limit 500 --interval 600 --comment-batch 10
```

## Docker Usage (Recommended)

### Quick Start with Docker

```bash
# Build the image
docker build -f Dockerfile -t reddit-scraper .

# Run with default settings
docker run --env-file .env reddit-scraper

# Run different subreddit with custom configuration
docker run --env-file .env reddit-scraper python reddit_scraper.py stocks --posts-limit 500 --interval 600

# Run high-volume configuration
docker run --env-file .env reddit-scraper python reddit_scraper.py wallstreetbets --posts-limit 2000 --interval 180 --comment-batch 30
```

### Docker Compose (Configurable)

Create a `.env.docker` file with your subreddit and configuration:

```bash
# Target Configuration
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=1000
SCRAPE_INTERVAL=300
COMMENT_BATCH=20

# Reddit API credentials
R_CLIENT_ID=your_client_id
R_CLIENT_SECRET=your_secret
R_USERNAME=your_username
R_PASSWORD=your_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/dbname
```

**Example configurations for different subreddits:**

**High-volume subreddit (.env.wallstreetbets):**

```bash
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=2000
SCRAPE_INTERVAL=180
COMMENT_BATCH=30
```

**Medium subreddit (.env.investing):**

```bash
TARGET_SUBREDDIT=investing
POSTS_LIMIT=1000
SCRAPE_INTERVAL=300
COMMENT_BATCH=20
```

**Low-activity subreddit (.env.pennystocks):**

```bash
TARGET_SUBREDDIT=pennystocks
POSTS_LIMIT=500
SCRAPE_INTERVAL=600
COMMENT_BATCH=10
```

Run with your chosen configuration:

```bash
# Start scraper with specific configuration
docker-compose -f docker-compose.yml --env-file .env.wallstreetbets up -d

# Switch to different subreddit
docker-compose -f docker-compose.yml --env-file .env.investing up -d

# View logs
docker-compose -f docker-compose.yml logs -f

# Stop scraper
docker-compose -f docker-compose.yml down
```

## Local Development

### Prerequisites

- Python 3.11+
- MongoDB Atlas account (cloud database)
- Reddit API credentials

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run scraper
python reddit_scraper.py wallstreetbets
```

## Command Line Options

```bash
# Basic usage - specify subreddit as first argument
python reddit_scraper.py SUBREDDIT_NAME [OPTIONS]

# Configuration options
--posts-limit 1000          # Posts to scrape per cycle (default: 1000)
--interval 300              # Seconds between cycles (default: 300)
--comment-batch 20          # Posts to process for comments per cycle (default: 20)

# Operational modes
--stats                     # Show statistics only
--comments-only             # Run comment scraping only
--metadata-only             # Update subreddit metadata only
```

**Complete Examples:**

```bash
# Show statistics for any subreddit
python reddit_scraper.py wallstreetbets --stats
python reddit_scraper.py stocks --stats

# Run with custom configuration for high-activity subreddit
python reddit_scraper.py wallstreetbets --posts-limit 2000 --interval 180 --comment-batch 30

# Run with conservative settings for smaller subreddit
python reddit_scraper.py pennystocks --posts-limit 500 --interval 600 --comment-batch 10

# Focus on specific tasks
python reddit_scraper.py investing --comments-only
python reddit_scraper.py cryptocurrency --metadata-only

# Different timing strategies
python reddit_scraper.py daytrading --interval 120    # Very frequent updates (2 min)
python reddit_scraper.py investing --interval 900     # Less frequent updates (15 min)
```

**Parameter Guidelines by Subreddit Size:**

| Subreddit Type            | Posts Limit | Interval  | Comment Batch | Example                           |
| ------------------------- | ----------- | --------- | ------------- | --------------------------------- |
| Very Active (>1M users)   | 2000-5000   | 120-300s  | 30-50         | `wallstreetbets`, `stocks`        |
| Active (100K-1M users)    | 1000-2000   | 300-600s  | 20-30         | `investing`, `cryptocurrency`     |
| Moderate (10K-100K users) | 500-1000    | 600-900s  | 10-20         | `pennystocks`, `SecurityAnalysis` |
| Small (<10K users)        | 100-500     | 900-1800s | 5-10          | Niche trading subreddits          |

## Configuration

All configuration is done via command line arguments or environment variables:

**Subreddit Selection:**

- Specify any subreddit name as the first argument
- No need to include "r/" prefix

**Timing Configuration:**

- `--interval`: Seconds between full scrape cycles (default: 300 = 5 minutes)
- Comment updates are automatic based on post age
- Subreddit metadata updates every 24 hours

**Volume Configuration:**

- `--posts-limit`: Hot posts to fetch per cycle (default: 1000)
- `--comment-batch`: Posts to process for comments per cycle (default: 20)

## Output Examples

```bash
$ python reddit_scraper.py wallstreetbets

🔗 Authenticated as: your_username
🎯 Target subreddit: r/wallstreetbets
⚙️  Configuration: {'scrape_interval': 300, 'posts_limit': 1000, 'posts_per_comment_batch': 20, 'subreddit_update_interval': 86400}

============================================================
SCRAPING STATISTICS FOR r/wallstreetbets
============================================================
Total posts: 5,432
Posts with initial comments: 4,891
Posts without initial comments: 541
Posts with recent updates: 1,203
Total comments: 45,672
Initial completion rate: 90.0%
Subreddit metadata: ✓
Metadata last updated: 2.3 hours ago
============================================================

🚀 Starting Reddit scraping for r/wallstreetbets
⏰ Scrape interval: 300 seconds
📊 Posts per scrape: 1000
💬 Comments batch size: 20 posts
🏢 Subreddit metadata interval: 24.0 hours

================================================================================
SCRAPE CYCLE #1 at 2024-01-20 15:30:00
================================================================================

============================================================
POST SCRAPING PHASE
============================================================
--- Scraping 1000 hot posts from r/wallstreetbets ---
Successfully scraped 1000 posts
Bulk operation: 23 new posts, 977 updated posts

============================================================
COMMENT SCRAPING PHASE
============================================================
Found 20 posts needing comment updates

Initial scrape for: TSLA to the moon! DD inside...
Found 0 existing comments
Found 45 new comments

Update for: Market crash incoming, here's why...
Found 150 existing comments
Found 23 new comments

Comment scraping completed: 20 posts (5 initial, 15 updates), 340 new comments

============================================================
SUBREDDIT METADATA PHASE
============================================================
Subreddit metadata updated 2.3 hours ago - next update in 21.7 hours

============================================================
CYCLE SUMMARY
============================================================
Posts scraped: 1000 (23 new)
Comments processed: 20 posts, 340 new comments
Subreddit metadata: No update needed
Cycle completed in 45.2 seconds

Waiting 300 seconds before next cycle...
```

## How It Works

The scraper runs **three integrated phases** in continuous cycles:

### **Phase 1: Posts Scraping** (Every 5 minutes)

- Fetches hot posts from your target subreddit
- Updates existing posts with new scores, comment counts
- Adds new posts that entered the hot list
- Preserves comment tracking status

### **Phase 2: Smart Comment Updates** (Continuous)

- **Never scraped posts**: Complete initial scrape (highest priority)
- **Recent posts (< 24h)**: Update every 6 hours
- **Older posts**: Update every 24 hours
- **Deduplication**: Only collects new comments, skips existing ones

### **Phase 3: Subreddit Metadata** (Every 24 hours)

- Tracks subscriber count, active users
- Community settings and rules
- Visual elements and descriptions

## Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd scrape-subreddit-data
```

## How to Use

There are two main scrapers that work independently:

### Posts and Comments Scraper

This is the main scraper that continuously collects hot posts from subreddits and downloads all their comments. It runs in cycles and never stops.

```bash
# Run continuously (this is the main scraper)
python scrape_reddit.py

# Show current statistics
python scrape_reddit.py --stats

# Only scrape comments for existing posts
python scrape_reddit.py --comments-only
```

What it does:

- Scrapes hot posts from r/wallstreetbets (you can change this in the code)
- Downloads all comments with full tree structure (replies to replies etc)
- Runs every 5 minutes by default
- Stores everything in `reddit_posts` and `reddit_comments` collections

<details>
<summary><strong>How scrape_reddit_posts.py Works (Technical Details)</strong></summary>

### Two-Phase Continuous Scraping System

The main scraper runs in continuous 5-minute cycles with two distinct phases:

#### Phase 1: Posts Scraping

```python
posts = scrape_hot_posts(SUB, POSTS_LIMIT)  # Gets 1000 hot posts
new_posts = save_posts_to_db(posts)         # Saves to database
```

**What happens:**

- Fetches current top 1000 hot posts from r/wallstreetbets
- Updates existing posts with new scores, comment counts, upvote ratios
- Adds new posts that entered the hot list
- Preserves comment tracking status for existing posts

#### Phase 2: Smart Comment Updates

```python
posts_processed, total_comments = scrape_comments_for_posts()
```

**instead of scraping comments once, it continuously updates them:**

**Smart Prioritization Logic:**

1. **Never scraped posts** (highest priority) - initial complete scrape
2. **Recent posts (< 24h old)** - update every 6 hours
3. **Older posts** - update every 24 hours

**Comment Deduplication Process:**

```python
# Before scraping, get existing comment IDs
existing_comment_ids = get_existing_comment_ids(post_id)

# Skip comments that already exist
if comment.id in existing_comment_ids:
    # Still check replies for new sub-comments
    process_replies_only()
```

### Database Schema for Tracking

Each post now tracks:

```json
{
  "post_id": "abc123",
  "comments_scraped": true,
  "initial_comments_scraped": true,
  "last_comment_fetch_time": "2024-01-20T15:30:00",
  "comments_scraped_at": "2024-01-20T12:00:00"
}
```

### Example Timeline for a Popular Post

```
Hour 0:  Post appears in hot → Initial scrape (all 50 comments)
Hour 6:  Still hot, 75 comments → Update scrape (25 new comments only)
Hour 12: Still hot, 120 comments → Update scrape (45 new comments only)
Hour 18: Falling in ranks, 140 comments → Update scrape (20 new comments)
Day 2:   Older post, 145 comments → Daily update (5 new comments)
```

### Performance Benefits

**Before (Traditional):**

- Scrape all comments once per post
- Miss all new comments added later
- Waste API calls re-scraping same comments

**After (Continuous Updates):**

- Only process new comments each update
- Capture live discussion as it happens
- 90% reduction in API calls for comment updates
- Complete comment history preserved

### Output Examples

```bash
--- Scraping comments for post abc123 ---
Found 150 existing comments for this post
Found 23 new comments (out of 173 processed)

Initial scrape for post: TSLA calls are printing money...
Found 0 existing comments for this post
Found 45 new comments (out of 45 processed)

Update for post: Market crash incoming...
Found 200 existing comments for this post
Found 12 new comments (out of 212 processed)

Comment scraping completed: 5 posts (2 initial, 3 updates), 80 new comments
```

### Statistics Tracking

The `--stats` command now shows:

- **Total posts**: All posts ever collected
- **Posts with initial comments**: Posts that have been fully scraped once
- **Posts without initial comments**: Posts waiting for first scrape
- **Posts with recent updates**: Posts updated in last 24 hours
- **Total comments**: All comments collected across all updates
- **Initial completion rate**: Percentage of posts with complete initial scrape

This system ensures you get **live-updating comment data** while being efficient and respectful to Reddit's API limits.

</details>

### Subreddit Metadata Scraper

This scraper collects information about the subreddit itself like subscriber count, description, settings, etc. It only runs when you tell it to and respects a 24-hour cooldown.

```bash
# Check and update subreddit info (respects 24h cooldown)
python scrape_subreddit_metadata.py

# Force update a specific subreddit
python scrape_subreddit_metadata.py --scrape wallstreetbets --force

# Update multiple subreddits
python scrape_subreddit_metadata.py --scrape wallstreetbets,stocks,investing

# Show metadata statistics
python scrape_subreddit_metadata.py --stats

# Get help
python scrape_subreddit_metadata.py --help
```

What it collects:

- Subscriber count and active users
- Subreddit description and rules
- Settings like what content is allowed
- Visual stuff like icons and banners
- Creation date, language, NSFW status
- Stores everything in `subreddit_metadata` collection
- Only updates every 24 hours to avoid spam

### Simple workflow

```bash
# 1. Start the main scraper in background
python scrape_reddit.py &

# 2. Update subreddit info once
python scrape_subreddit_metadata.py --scrape wallstreetbets

# 3. Check what you collected
python scrape_reddit.py --stats
python scrape_subreddit_metadata.py --stats
```

## Docker Usage

### Main Posts/Comments Scraper

You'll need a separate Dockerfile for the main scraper (create `Dockerfile.main`):

```bash
# Build and run main scraper
docker build -f Dockerfile.main -t reddit-scraper .
docker run --env-file .env reddit-scraper
```

### Metadata Scraper

```bash
# Build metadata scraper
docker build -f Dockerfile.metadata -t reddit-metadata-scraper .

# Run with different commands
docker run --env-file .env reddit-metadata-scraper python scrape_subreddit_metadata.py --stats
docker run --env-file .env reddit-metadata-scraper python scrape_subreddit_metadata.py --scrape wallstreetbets
```

### Docker Compose

For metadata scraper only:

```bash
docker-compose -f docker-compose.metadata.yml up -d
```

## Local Development

### Prerequisites

- Python 3.11+
- MongoDB Atlas account (cloud database)
- Reddit API credentials

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
# Edit .env with your credentials and MongoDB Atlas URI

# Run scrapers
python scrape_reddit.py
python scrape_subreddit_metadata.py
```

## Configuration

Edit the Python files to customize:

**For main scraper (`scrape_reddit.py`):**

```python
SUB = "wallstreetbets"              # Target subreddit
SCRAPE_INTERVAL = 300               # Seconds between cycles
POSTS_LIMIT = 1000                  # Posts per scrape
POSTS_PER_COMMENT_BATCH = 20        # Comments batch size
```

**For metadata scraper (`scrape_subreddit_metadata.py`):**

```python
SUBREDDIT_SCRAPE_INTERVAL = 86400   # 24 hours between updates
```

## Database Schema

### Posts Collection (reddit_posts)

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
  "comments_scraped_at": "2022-01-20T12:30:00"
}
```

### Comments Collection (reddit_comments)

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
  "created_datetime": "2022-01-20T12:01:40"
}
```

### Subreddit Metadata Collection (subreddit_metadata)

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
  "created_datetime": "2009-02-13T23:31:30",
  "quarantine": false,
  "allow_images": true,
  "allow_videos": true,
  "allow_polls": true,
  "scraped_at": "2022-01-20T12:00:00",
  "last_updated": "2022-01-20T12:00:00"
}
```

## Monitoring

### Check Docker containers

```bash
# See running containers
docker ps

# View logs
docker logs reddit-metadata-scraper

# Follow logs in real-time
docker logs -f reddit-metadata-scraper
```

### Get statistics

```bash
# From running container
docker exec reddit-metadata-scraper python scrape_subreddit_metadata.py --stats
```

## Troubleshooting

### Common Issues

**Reddit API Rate Limits**

- The scrapers handle rate limits automatically
- If you hit limits often, reduce POSTS_LIMIT in the main scraper

**MongoDB Connection**

- Make sure your MONGODB_URI is correct
- Check that your IP is whitelisted in MongoDB Atlas
- Verify your database user has proper permissions

**Reddit Authentication**

- Double-check credentials in .env file
- Make sure your Reddit app has the right permissions
- User agent should be descriptive and unique

### Logs

```bash
# For Docker containers
docker logs reddit-metadata-scraper

# Check if containers are healthy
docker ps
```

## Performance

- Bulk operations: Much faster database writes
- Rate limiting: Automatic API throttling to avoid bans
- Efficient indexing: Fast MongoDB queries
- Memory efficient: Processes data in streams

The scrapers are designed to be respectful to Reddit's API and run reliably for long periods.

## License

MIT License - see LICENSE file for details.
