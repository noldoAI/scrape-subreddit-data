"""
Prometheus metrics module for Reddit Scraper.
Exposes metrics for Grafana/Prometheus monitoring.
"""

from prometheus_client import Gauge, Counter, Info, generate_latest, CONTENT_TYPE_LATEST
import logging

logger = logging.getLogger("metrics")

# =============================================================================
# PER-SUBREDDIT METRICS (from MongoDB)
# =============================================================================

# Database totals
posts_total = Gauge(
    'reddit_scraper_posts_total',
    'Total posts in database per subreddit',
    ['subreddit']
)

comments_total = Gauge(
    'reddit_scraper_comments_total',
    'Total comments in database per subreddit',
    ['subreddit']
)

# Scraper collection metrics
posts_collected = Gauge(
    'reddit_scraper_posts_collected',
    'Total posts collected by scraper',
    ['subreddit']
)

comments_collected = Gauge(
    'reddit_scraper_comments_collected',
    'Total comments collected by scraper',
    ['subreddit']
)

# Collection rates
posts_per_hour = Gauge(
    'reddit_scraper_posts_per_hour',
    'Posts collected per hour',
    ['subreddit']
)

comments_per_hour = Gauge(
    'reddit_scraper_comments_per_hour',
    'Comments collected per hour',
    ['subreddit']
)

# =============================================================================
# SCRAPER STATUS METRICS
# =============================================================================

scraper_status = Gauge(
    'reddit_scraper_status',
    'Scraper status (1=running, 0=stopped, -1=failed)',
    ['subreddit', 'scraper_type']
)

scraper_cycles = Gauge(
    'reddit_scraper_cycles_total',
    'Total cycles completed by scraper',
    ['subreddit']
)

scraper_restarts = Gauge(
    'reddit_scraper_restarts_total',
    'Total restarts for scraper',
    ['subreddit', 'scraper_type']
)

cycle_duration = Gauge(
    'reddit_scraper_last_cycle_duration_seconds',
    'Last cycle duration in seconds',
    ['subreddit']
)

# =============================================================================
# ERROR METRICS
# =============================================================================

errors_unresolved = Gauge(
    'reddit_scraper_errors_unresolved',
    'Unresolved errors per subreddit',
    ['subreddit']
)

errors_total = Gauge(
    'reddit_scraper_errors_total',
    'Total errors by type',
    ['subreddit', 'error_type']
)

# =============================================================================
# RATE LIMIT METRICS
# =============================================================================

rate_limit_remaining = Gauge(
    'reddit_rate_limit_remaining',
    'Reddit API remaining requests',
    ['subreddit']
)

# =============================================================================
# SYSTEM HEALTH METRICS
# =============================================================================

scraper_up = Gauge(
    'reddit_scraper_up',
    'API server health (1=up, 0=down)'
)

database_connected = Gauge(
    'reddit_database_connected',
    'MongoDB connection status (1=connected, 0=disconnected)'
)

docker_available = Gauge(
    'reddit_docker_available',
    'Docker daemon availability (1=available, 0=unavailable)'
)

active_scrapers = Gauge(
    'reddit_scrapers_active',
    'Number of active scrapers',
    ['scraper_type']
)

# =============================================================================
# INFO METRIC
# =============================================================================

scraper_info = Info(
    'reddit_scraper',
    'Reddit scraper version and build information'
)


def update_metrics_from_db(db, posts_collection, comments_collection, scrapers_collection, errors_collection):
    """
    Update all Prometheus metrics from MongoDB state.
    Called on each /metrics request.
    """
    try:
        # Get all unique subreddits from posts
        subreddits = posts_collection.distinct("subreddit")

        # Per-subreddit database counts
        for sub in subreddits:
            # Count posts and comments
            post_count = posts_collection.count_documents({"subreddit": sub})
            comment_count = comments_collection.count_documents({"subreddit": sub})

            posts_total.labels(subreddit=sub).set(post_count)
            comments_total.labels(subreddit=sub).set(comment_count)

        # Get scraper metrics from reddit_scrapers collection
        scrapers = list(scrapers_collection.find({}))

        posts_scraper_count = 0
        comments_scraper_count = 0

        for scraper in scrapers:
            subreddit = scraper.get("subreddit", "unknown")
            scraper_type = scraper.get("scraper_type", "posts")
            status = scraper.get("status", "unknown")

            # Handle multi-subreddit scrapers
            subreddits_list = scraper.get("subreddits", [subreddit])
            if isinstance(subreddits_list, list) and len(subreddits_list) > 1:
                # For multi-sub scrapers, use container identifier
                subreddit = f"multi-{len(subreddits_list)}subs"

            # Status mapping: running=1, stopped=0, failed=-1
            status_value = 1 if status == "running" else (-1 if status == "failed" else 0)
            scraper_status.labels(subreddit=subreddit, scraper_type=scraper_type).set(status_value)

            # Count active scrapers
            if status == "running":
                if scraper_type == "posts":
                    posts_scraper_count += 1
                else:
                    comments_scraper_count += 1

            # Restart count
            restart_count = scraper.get("restart_count", 0)
            scraper_restarts.labels(subreddit=subreddit, scraper_type=scraper_type).set(restart_count)

            # Scraper metrics (from metrics subdocument)
            metrics = scraper.get("metrics", {})
            if metrics:
                # Collection totals
                if metrics.get("total_posts_collected"):
                    posts_collected.labels(subreddit=subreddit).set(metrics["total_posts_collected"])
                if metrics.get("total_comments_collected"):
                    comments_collected.labels(subreddit=subreddit).set(metrics["total_comments_collected"])

                # Rates
                if metrics.get("posts_per_hour"):
                    posts_per_hour.labels(subreddit=subreddit).set(metrics["posts_per_hour"])
                if metrics.get("comments_per_hour"):
                    comments_per_hour.labels(subreddit=subreddit).set(metrics["comments_per_hour"])

                # Cycles
                if metrics.get("total_cycles"):
                    scraper_cycles.labels(subreddit=subreddit).set(metrics["total_cycles"])

                # Last cycle duration
                if metrics.get("last_cycle_duration"):
                    cycle_duration.labels(subreddit=subreddit).set(metrics["last_cycle_duration"])

        # Set active scraper counts
        active_scrapers.labels(scraper_type="posts").set(posts_scraper_count)
        active_scrapers.labels(scraper_type="comments").set(comments_scraper_count)

        # Error metrics from reddit_scrape_errors collection
        error_pipeline = [
            {"$match": {"resolved": {"$ne": True}}},
            {"$group": {
                "_id": {"subreddit": "$subreddit", "error_type": "$error_type"},
                "count": {"$sum": 1}
            }}
        ]
        error_results = list(errors_collection.aggregate(error_pipeline))

        # Reset error gauges first
        errors_unresolved._metrics.clear()
        errors_total._metrics.clear()

        # Aggregate errors by subreddit
        subreddit_errors = {}
        for err in error_results:
            sub = err["_id"].get("subreddit", "unknown")
            err_type = err["_id"].get("error_type", "unknown")
            count = err["count"]

            subreddit_errors[sub] = subreddit_errors.get(sub, 0) + count
            errors_total.labels(subreddit=sub, error_type=err_type).set(count)

        for sub, count in subreddit_errors.items():
            errors_unresolved.labels(subreddit=sub).set(count)

        logger.debug(f"Updated metrics for {len(subreddits)} subreddits, {len(scrapers)} scrapers")

    except Exception as e:
        logger.error(f"Error updating metrics from DB: {e}")
        raise


def get_metrics():
    """Generate Prometheus metrics in text format."""
    return generate_latest()


def init_metrics(version="1.5.0"):
    """Initialize static metrics like version info."""
    scraper_info.info({
        'version': version,
        'service': 'reddit-scraper-api'
    })
