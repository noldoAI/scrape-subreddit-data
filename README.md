# Reddit Scraper

A comprehensive Reddit scraping system that collects posts, comments, and subreddit metadata with proper rate limiting and tree structure preservation.

## Features

- Unified scraping: Posts and comments in optimized cycles
- Subreddit metadata: Track subscriber counts, settings, and community info
- Bulk operations: High-performance MongoDB operations
- Rate limiting: Automatic Reddit API rate limit handling
- Comment trees: Preserves hierarchical comment structure
- Analytics: Built-in statistics and progress tracking
- Docker ready: Easy deployment with Docker Compose

## Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd scrape-subreddit-data
```

### 2. Set up environment variables

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
