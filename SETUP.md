# Reddit Scraper - Easy Setup Guide

Simple tool to scrape any subreddit's posts and comments continuously using Docker.

## What You Need

1. **Reddit Account** - to get API access
2. **MongoDB Atlas Account** - free cloud database
3. **Docker** - to run the scraper
4. **5 minutes** - to set everything up

## Step 1: Get Reddit API Credentials

1. Go to https://www.reddit.com/prefs/apps
2. Click "Create App" or "Create Another App"
3. Fill out:
   - **Name**: `MyRedditScraper` (or anything you want)
   - **App type**: Select "script"
   - **Description**: Leave blank
   - **About URL**: Leave blank
   - **Redirect URI**: `http://localhost:8080`
4. Click "Create app"
5. Note down:
   - **Client ID**: The random string under your app name
   - **Client Secret**: The longer random string labeled "secret"

## Step 2: Get MongoDB Atlas (Free Database)

1. Go to https://www.mongodb.com/atlas
2. Click "Try Free" and sign up
3. Create a new cluster (choose the free tier)
4. Set up database access:
   - Go to "Database Access" → "Add New Database User"
   - Create username/password (remember these!)
   - Give "Read and write to any database" permissions
5. Set up network access:
   - Go to "Network Access" → "Add IP Address"
   - Click "Allow Access from Anywhere" (for simplicity)
6. Get your connection string:
   - Go to "Clusters" → "Connect" → "Connect your application"
   - Copy the connection string (looks like `mongodb+srv://...`)
   - Replace `<password>` with your actual password

## Step 3: Create Environment File

Create a file called `.env` in your project folder:

```bash
# Reddit API (from Step 1)
R_CLIENT_ID=your_client_id_here
R_CLIENT_SECRET=your_client_secret_here
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername

# MongoDB Atlas (from Step 2)
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/reddit_data
```

## Step 4: Choose Your Subreddit & Settings

Create different environment files for different subreddits:

### For High-Activity Subreddits (wallstreetbets, stocks)

Create `.env.wallstreetbets`:

```bash
# Subreddit Configuration
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=2000
SCRAPE_INTERVAL=180
COMMENT_BATCH=30

# Reddit API (copy from your main .env file)
R_CLIENT_ID=your_client_id_here
R_CLIENT_SECRET=your_client_secret_here
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/reddit_data
```

### For Medium Subreddits (investing, cryptocurrency)

Create `.env.investing`:

```bash
# Subreddit Configuration
TARGET_SUBREDDIT=investing
POSTS_LIMIT=1000
SCRAPE_INTERVAL=300
COMMENT_BATCH=20

# Reddit API (copy from your main .env file)
R_CLIENT_ID=your_client_id_here
R_CLIENT_SECRET=your_client_secret_here
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/reddit_data
```

### For Small Subreddits (pennystocks, niche topics)

Create `.env.pennystocks`:

```bash
# Subreddit Configuration
TARGET_SUBREDDIT=pennystocks
POSTS_LIMIT=500
SCRAPE_INTERVAL=600
COMMENT_BATCH=10

# Reddit API (copy from your main .env file)
R_CLIENT_ID=your_client_id_here
R_CLIENT_SECRET=your_client_secret_here
R_USERNAME=your_reddit_username
R_PASSWORD=your_reddit_password
R_USER_AGENT=RedditScraper/1.0 by YourUsername
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/reddit_data
```

## Step 5: Run with Docker

### Quick Start (any subreddit)

```bash
# Build the scraper
docker-compose build

# Run wallstreetbets
docker-compose --env-file .env.wallstreetbets up -d

# Run investing
docker-compose --env-file .env.investing up -d

# Run pennystocks
docker-compose --env-file .env.pennystocks up -d
```

### Check if it's working

```bash
# See logs
docker-compose logs -f

# Check statistics
docker-compose exec reddit-scraper python reddit_scraper.py wallstreetbets --stats
```

### Stop scraping

```bash
docker-compose down
```

## Settings Explained

| Setting            | What it does                  | Recommended Values                          |
| ------------------ | ----------------------------- | ------------------------------------------- |
| `TARGET_SUBREDDIT` | Which subreddit to scrape     | Any subreddit name (no r/)                  |
| `POSTS_LIMIT`      | How many hot posts to get     | High activity: 2000, Medium: 1000, Low: 500 |
| `SCRAPE_INTERVAL`  | Seconds between updates       | High activity: 180, Medium: 300, Low: 600   |
| `COMMENT_BATCH`    | Comments to process per cycle | High activity: 30, Medium: 20, Low: 10      |

## Popular Subreddit Configurations

**Copy-paste ready configs:**

### Financial/Trading

```bash
# WallStreetBets
TARGET_SUBREDDIT=wallstreetbets
POSTS_LIMIT=2000
SCRAPE_INTERVAL=180
COMMENT_BATCH=30

# Stocks
TARGET_SUBREDDIT=stocks
POSTS_LIMIT=1500
SCRAPE_INTERVAL=240
COMMENT_BATCH=25

# Investing
TARGET_SUBREDDIT=investing
POSTS_LIMIT=1000
SCRAPE_INTERVAL=300
COMMENT_BATCH=20

# Cryptocurrency
TARGET_SUBREDDIT=cryptocurrency
POSTS_LIMIT=1500
SCRAPE_INTERVAL=200
COMMENT_BATCH=25

# PennyStocks
TARGET_SUBREDDIT=pennystocks
POSTS_LIMIT=500
SCRAPE_INTERVAL=600
COMMENT_BATCH=10
```

### Tech/Gaming

```bash
# Technology
TARGET_SUBREDDIT=technology
POSTS_LIMIT=1200
SCRAPE_INTERVAL=300
COMMENT_BATCH=20

# Gaming
TARGET_SUBREDDIT=gaming
POSTS_LIMIT=1500
SCRAPE_INTERVAL=240
COMMENT_BATCH=25

# Programming
TARGET_SUBREDDIT=programming
POSTS_LIMIT=800
SCRAPE_INTERVAL=400
COMMENT_BATCH=15
```

## Quick Commands

```bash
# Start scraping wallstreetbets
docker-compose --env-file .env.wallstreetbets up -d

# Switch to different subreddit
docker-compose down
docker-compose --env-file .env.investing up -d

# View live logs
docker-compose logs -f

# Check what you've collected
docker-compose exec reddit-scraper python reddit_scraper.py TARGET_SUBREDDIT --stats

# Stop everything
docker-compose down
```

## What Gets Collected

The scraper collects:

### Posts

- Title, URL, content
- Upvotes, comments count
- Author, subreddit
- Creation time

### Comments

- Comment text and replies
- Upvotes, author
- Comment tree structure
- Reply chains

### Subreddit Info

- Subscriber count
- Description, rules
- Community settings

All data goes to your MongoDB Atlas database in these collections:

- `reddit_posts`
- `reddit_comments`
- `subreddit_metadata`

## Troubleshooting

**"Can't connect to MongoDB"**

- Check your connection string has the right password
- Make sure you allowed all IP addresses in Atlas

**"Reddit API errors"**

- Check your Reddit credentials are correct
- Make sure your Reddit app is set to "script" type

**"Too many API calls"**

- Increase `SCRAPE_INTERVAL` (make it higher)
- Decrease `POSTS_LIMIT` and `COMMENT_BATCH`

**"No new data"**

- Some subreddits are slow - that's normal
- Check logs: `docker-compose logs -f`

## Advanced Usage

### Multiple Subreddits at Once

Run different subreddits in separate containers:

```bash
# Terminal 1
docker-compose --env-file .env.wallstreetbets up

# Terminal 2
docker-compose --env-file .env.investing up

# Terminal 3
docker-compose --env-file .env.cryptocurrency up
```

### Custom Subreddit

For any subreddit not listed above:

1. Create `.env.YOURSUBREDDIT`
2. Set `TARGET_SUBREDDIT=yoursubreddit`
3. Choose settings based on activity level:
   - **Very active** (>1M users): Use wallstreetbets settings
   - **Active** (100K-1M users): Use investing settings
   - **Small** (<100K users): Use pennystocks settings

### Without Docker

If you prefer running directly:

```bash
# Install requirements
pip install -r requirements.txt

# Run with any subreddit
python reddit_scraper.py SUBREDDIT_NAME --posts-limit 1000 --interval 300 --comment-batch 20
```

## Need Help?

1. **Check logs first**: `docker-compose logs -f`
2. **Verify credentials**: Make sure Reddit API and MongoDB are correct
3. **Test connection**: Use `--stats` to see if basic connection works
4. **Start small**: Try a small subreddit first with conservative settings

That's it! You should now have continuous Reddit scraping running for any subreddit you want.
