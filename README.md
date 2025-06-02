# Reddit Scraper

A comprehensive Reddit scraping system that collects posts, comments, and subreddit metadata with proper rate limiting and tree structure preservation.

## Features

- 🚀 **Unified scraping**: Posts and comments in optimized cycles
- 📊 **Subreddit metadata**: Track subscriber counts, settings, and community info
- ⚡ **Bulk operations**: High-performance MongoDB operations
- 🛡️ **Rate limiting**: Automatic Reddit API rate limit handling
- 🌳 **Comment trees**: Preserves hierarchical comment structure
- 📈 **Analytics**: Built-in statistics and progress tracking
- 🐳 **Docker ready**: Easy deployment with Docker Compose

## Quick Start with Docker

### 1. Clone the repository

```bash
git clone <repository-url>
cd scrape-subreddit-data
```

### 2. Set up environment variables

Create a `.env` file with your Reddit API credentials:

```bash
# Reddit API Credentials (get from https://www.reddit.com/prefs/apps)
R_CLIENT_ID=your_reddit_client_id
R_CLIENT_SECRET=your_reddit_client_secret
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
```

### 3. Deploy with Docker Compose

```bash
# Start all services (scraper + MongoDB + Mongo Express)
docker-compose up -d

# View logs
docker-compose logs -f reddit-scraper

# Stop services
docker-compose down
```

### 4. Access services

- **MongoDB**: `localhost:27017`
- **Mongo Express** (Web UI): `http://localhost:8081` (admin/admin123)

## How to Scrape Reddit Data

### 📝 **Posts and Comments** (Main Scraper)

Collects hot posts from subreddits and their comment threads:

```bash
# Continuous scraping (runs forever, scraping posts + comments)
python scrape_reddit.py

# Show current statistics
python scrape_reddit.py --stats

# Catch up on comments only (for existing posts)
python scrape_reddit.py --comments-only
```

**What it does:**

- Scrapes hot posts from r/wallstreetbets (configurable)
- Downloads all comments with full tree structure
- Updates every 5 minutes by default
- Stores in `reddit_posts` and `reddit_comments` collections

### 🏢 **Subreddit Metadata** (Separate Scraper)

Collects subreddit information like subscriber count, description, settings:

```bash
# Check and update wallstreetbets metadata (respects 24h interval)
python scrape_subreddit_metadata.py

# Force update a specific subreddit
python scrape_subreddit_metadata.py --scrape wallstreetbets --force

# Scrape multiple subreddits at once
python scrape_subreddit_metadata.py --scrape wallstreetbets,stocks,investing

# Show metadata statistics
python scrape_subreddit_metadata.py --stats

# Get help
python scrape_subreddit_metadata.py --help
```

**What it collects:**

- Subscriber count, active users
- Subreddit description, rules, settings
- Visual elements (icons, banners)
- Creation date, language, NSFW status
- Stores in `subreddit_metadata` collection
- Updates every 24 hours automatically

### 🚀 **Quick Example Workflow**

```bash
# 1. Start main scraper (posts + comments)
python scrape_reddit.py &

# 2. Update subreddit metadata once
python scrape_subreddit_metadata.py --scrape wallstreetbets

# 3. Check what you've collected
python scrape_reddit.py --stats
python scrape_subreddit_metadata.py --stats
```

## Manual Docker Build

```bash
# Build the image
docker build -t reddit-scraper .

# Run with environment file
docker run --env-file .env reddit-scraper

# Run with custom command
docker run --env-file .env reddit-scraper python scrape_reddit.py --stats
```

## Local Development

### Prerequisites

- Python 3.11+
- MongoDB
- Reddit API credentials

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
# Edit .env with your credentials

# Run the scraper
python scrape_reddit.py
```

## Usage

### Main Commands

```bash
# Full continuous scraping (default)
python scrape_reddit.py

# Show statistics only
python scrape_reddit.py --stats

# Scrape comments only (catch up mode)
python scrape_reddit.py --comments-only
```

### Reconstruct Posts

```bash
# Interactive mode
python reconstruct_posts.py

# Command line
python reconstruct_posts.py POST_ID
python reconstruct_posts.py POST_ID --save
python reconstruct_posts.py POST_ID --json
```

## Configuration

Edit `scrape_reddit.py` to customize:

```python
SUB = "wallstreetbets"              # Target subreddit
SCRAPE_INTERVAL = 300               # Seconds between cycles
POSTS_LIMIT = 1000                  # Posts per scrape
POSTS_PER_COMMENT_BATCH = 20        # Comments batch size
```

## Database Schema

### Posts Collection (`reddit_posts`)

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

### Comments Collection (`reddit_comments`)

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

### Subreddit Metadata Collection (`subreddit_metadata`)

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

## Docker Services

### reddit-scraper

- **Purpose**: Main scraping application
- **Restart**: Unless stopped
- **Health check**: Every 30 seconds

### mongodb

- **Image**: mongo:7.0
- **Port**: 27017
- **Volume**: Persistent data storage

### mongo-express

- **Purpose**: Web-based MongoDB admin interface
- **Port**: 8081
- **Credentials**: admin/admin123

## Monitoring

### Health Checks

```bash
# Check container health
docker ps

# View detailed logs
docker-compose logs reddit-scraper

# Monitor in real-time
docker-compose logs -f reddit-scraper
```

### Statistics

```bash
# Get current stats
docker-compose exec reddit-scraper python scrape_reddit.py --stats
```

## Troubleshooting

### Common Issues

1. **Reddit API Rate Limits**

   - The scraper automatically handles rate limits
   - Reduce `POSTS_LIMIT` if hitting limits frequently

2. **MongoDB Connection**

   - Ensure MongoDB is running: `docker-compose ps`
   - Check connection string in environment variables

3. **Reddit Authentication**
   - Verify credentials in `.env` file
   - Check Reddit app permissions

### Logs

```bash
# Application logs
docker-compose logs reddit-scraper

# MongoDB logs
docker-compose logs mongodb

# All services
docker-compose logs
```

## Security

- ✅ Non-root user in container
- ✅ Environment variables for secrets
- ✅ No hardcoded credentials
- ✅ Minimal base image (Python slim)

## Performance

- **Bulk operations**: ~1000x fewer database operations
- **Rate limiting**: Automatic API throttling
- **Efficient indexing**: Optimized MongoDB queries
- **Memory efficient**: Streaming data processing

## License

MIT License - see LICENSE file for details.
