#!/usr/bin/env python3
"""
Reddit API Usage Tracker

Stores HTTP request counts and costs to MongoDB for billing/monitoring.
HTTP requests are counted by CountingSession at the transport layer.
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pymongo

from config import DATABASE_NAME, LOGGING_CONFIG

# Configure logging
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True
)
logger = logging.getLogger("api-usage-tracker")

# API Usage Configuration
API_USAGE_CONFIG = {
    "collection_name": "reddit_api_usage",
    "flush_interval": 60,           # Seconds between DB writes
    "retention_days": 30,           # Days to keep historical data (TTL)
    "cost_per_1000_requests": 0.24, # Reddit API pricing
}


class APIUsageTracker:
    """
    Stores Reddit API usage data (HTTP request counts and costs) to MongoDB.

    Usage:
        tracker = APIUsageTracker(subreddit="wallstreetbets", scraper_type="posts", db=db)

        # At end of cycle, pass HTTP stats from CountingSession
        cycle_stats = http_session.reset_cycle()
        tracker.flush_to_db(rate_limit_info, http_stats=cycle_stats)
    """

    def __init__(
        self,
        subreddit: str,
        scraper_type: str,
        db: Optional[pymongo.database.Database] = None,
        container_id: Optional[str] = None
    ):
        """
        Initialize the tracker.

        Args:
            subreddit: Subreddit name(s) being scraped (comma-separated for multi)
            scraper_type: "posts" or "comments"
            db: MongoDB database instance (optional, will create if not provided)
            container_id: Docker container ID (optional)
        """
        self.subreddit = subreddit
        self.scraper_type = scraper_type
        self.container_id = container_id or os.getenv("HOSTNAME", "unknown")
        self.cycle_start_time = time.time()

        # MongoDB connection
        if db is not None:
            self.db = db
        else:
            # Create our own connection if not provided
            mongodb_uri = os.getenv("MONGODB_URI")
            if mongodb_uri:
                client = pymongo.MongoClient(mongodb_uri)
                self.db = client[DATABASE_NAME]
            else:
                self.db = None
                logger.warning("No MongoDB URI provided, tracking will be disabled")

        self.collection = self.db[API_USAGE_CONFIG["collection_name"]] if self.db is not None else None

        # Ensure indexes exist (run once)
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes if they don't exist."""
        if self.collection is None:
            return

        try:
            # Compound index for querying by subreddit and time
            self.collection.create_index(
                [("subreddit", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)],
                name="subreddit_timestamp"
            )

            # Index for hourly aggregation
            self.collection.create_index(
                [("hour_bucket", pymongo.ASCENDING)],
                name="hour_bucket"
            )

            # Index for daily aggregation
            self.collection.create_index(
                [("day_bucket", pymongo.ASCENDING)],
                name="day_bucket"
            )

            # TTL index for automatic cleanup (30 days)
            self.collection.create_index(
                "timestamp",
                name="ttl_cleanup",
                expireAfterSeconds=API_USAGE_CONFIG["retention_days"] * 24 * 60 * 60
            )

            logger.debug("API usage indexes ensured")
        except Exception as e:
            logger.warning(f"Failed to create indexes: {e}")

    def flush_to_db(self, rate_limit_info: Optional[dict] = None, http_stats: Optional[dict] = None) -> bool:
        """
        Save HTTP request stats to MongoDB.

        Args:
            rate_limit_info: Rate limit snapshot from reddit.auth.limits
            http_stats: Actual HTTP request stats from CountingSession

        Returns:
            True if flush succeeded, False otherwise
        """
        if self.collection is None:
            logger.debug("No MongoDB collection, skipping flush")
            return False

        # Skip if no HTTP stats
        if http_stats is None or http_stats.get('cycle_requests', 0) == 0:
            logger.debug("No requests to flush")
            return True

        now = datetime.now(timezone.utc)

        # Calculate time buckets for aggregation
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)
        day_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Build document with HTTP stats
        actual_requests = http_stats.get('cycle_requests', 0)
        cost_usd = http_stats.get('cycle_cost_usd', 0.0)

        doc = {
            "subreddit": self.subreddit,
            "scraper_type": self.scraper_type,
            "container_id": self.container_id,
            "timestamp": now,
            "hour_bucket": hour_bucket,
            "day_bucket": day_bucket,
            "actual_http_requests": actual_requests,
            "estimated_cost_usd": cost_usd,
            "cycle_duration_seconds": round(time.time() - self.cycle_start_time, 2)
        }

        # Add rate limit info if available
        if rate_limit_info:
            doc["rate_limit"] = {
                "remaining": rate_limit_info.get("remaining"),
                "used": rate_limit_info.get("used"),
                "reset_in_seconds": rate_limit_info.get("reset_in_seconds")
            }

        try:
            self.collection.insert_one(doc)
            logger.debug(f"Flushed usage: {actual_requests} requests, ${cost_usd:.4f}")

            # Reset cycle timer
            self.cycle_start_time = time.time()
            return True

        except Exception as e:
            logger.error(f"Failed to flush usage to DB: {e}")
            return False


def get_usage_stats(db, subreddit: Optional[str] = None) -> dict:
    """
    Get aggregated usage statistics.

    Args:
        db: MongoDB database instance
        subreddit: Optional subreddit filter

    Returns:
        Aggregated usage stats
    """
    collection = db[API_USAGE_CONFIG["collection_name"]]

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_ago = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)

    # Build match stage
    match_stage = {"timestamp": {"$gte": today_start}}
    if subreddit:
        match_stage["subreddit"] = subreddit

    # Aggregate for today
    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": None,
                "actual_http_requests": {"$sum": {"$ifNull": ["$actual_http_requests", 0]}},
                "total_cost_usd": {"$sum": {"$ifNull": ["$estimated_cost_usd", 0]}},
            }
        }
    ]

    result = list(collection.aggregate(pipeline))
    today_stats = result[0] if result else {}

    # Aggregate for last hour
    match_stage["timestamp"] = {"$gte": hour_ago}
    pipeline[0]["$match"] = match_stage

    result = list(collection.aggregate(pipeline))
    hour_stats = result[0] if result else {}

    # Get requests by subreddit (if not filtered)
    requests_by_subreddit = {}
    if not subreddit:
        pipeline = [
            {"$match": {"timestamp": {"$gte": today_start}}},
            {
                "$group": {
                    "_id": "$subreddit",
                    "requests": {"$sum": {"$ifNull": ["$actual_http_requests", 0]}}
                }
            },
            {"$sort": {"requests": -1}},
            {"$limit": 20}
        ]
        for doc in collection.aggregate(pipeline):
            requests_by_subreddit[doc["_id"]] = doc["requests"]

    # Get latest rate limit info
    rate_limit = None
    if subreddit:
        latest = collection.find_one(
            {"subreddit": subreddit, "rate_limit": {"$exists": True}},
            sort=[("timestamp", pymongo.DESCENDING)]
        )
        if latest and "rate_limit" in latest:
            rate_limit = {
                **latest["rate_limit"],
                "last_updated": latest["timestamp"].isoformat() if latest.get("timestamp") else None
            }

    # Calculate costs
    actual_requests_today = today_stats.get("actual_http_requests", 0)
    actual_requests_hour = hour_stats.get("actual_http_requests", 0)
    cost_today = today_stats.get("total_cost_usd", 0)
    cost_hour = hour_stats.get("total_cost_usd", 0)

    return {
        "actual_http_requests_today": actual_requests_today,
        "actual_http_requests_hour": actual_requests_hour,
        "requests_by_subreddit": requests_by_subreddit,
        "rate_limit": rate_limit,
        "cost_usd_today": round(cost_today, 4),
        "cost_usd_hour": round(cost_hour, 4),
    }


def get_usage_trends(
    db,
    subreddit: Optional[str] = None,
    hours: int = 24
) -> list:
    """
    Get hourly usage trends.

    Args:
        db: MongoDB database instance
        subreddit: Optional subreddit filter
        hours: Number of hours to look back

    Returns:
        List of hourly stats
    """
    collection = db[API_USAGE_CONFIG["collection_name"]]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    match_stage = {"timestamp": {"$gte": cutoff}}
    if subreddit:
        match_stage["subreddit"] = subreddit

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$hour_bucket",
                "requests": {"$sum": {"$ifNull": ["$actual_http_requests", 0]}},
                "cost_usd": {"$sum": {"$ifNull": ["$estimated_cost_usd", 0]}}
            }
        },
        {"$sort": {"_id": 1}}
    ]

    return [
        {
            "hour": doc["_id"].isoformat() if doc["_id"] else None,
            "requests": doc["requests"],
            "cost_usd": round(doc["cost_usd"], 4)
        }
        for doc in collection.aggregate(pipeline)
    ]
