#!/usr/bin/env python3
"""
Reddit API Usage Tracker

Tracks Reddit API calls with timing, categorization, and historical storage in MongoDB.
Provides per-request visibility, batched writes, and in-memory stats.
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

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
    "batch_size": 100,              # Max records per flush
}

# Call type constants
CALL_TYPES = {
    "posts_fetch": "posts_fetch",           # subreddit.new(), .top(), .rising(), etc.
    "comments_fetch": "comments_fetch",     # submission.comments
    "comments_expand": "comments_expand",   # replace_more() calls
    "metadata_fetch": "metadata_fetch",     # subreddit rules, requirements, info
    "auth_check": "auth_check",             # reddit.user.me()
    "rate_limit_check": "rate_limit_check", # reddit.auth.limits access
}


class APIUsageTracker:
    """
    Tracks Reddit API calls and stores usage data in MongoDB.

    Usage:
        tracker = APIUsageTracker(subreddit="wallstreetbets", scraper_type="posts", db=db)

        # Track a call
        start = time.time()
        posts = subreddit.new(limit=100)
        tracker.track_call("posts_fetch", "subreddit.new", success=True,
                          response_time_ms=(time.time() - start) * 1000)

        # Flush to DB at end of cycle
        tracker.flush_to_db(rate_limit_info)
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
                logger.warning("No MongoDB URI provided, tracking will be in-memory only")

        self.collection = self.db[API_USAGE_CONFIG["collection_name"]] if self.db else None

        # In-memory tracking for current cycle
        self._reset_cycle_stats()

        # Ensure indexes exist (run once)
        self._ensure_indexes()

    def _reset_cycle_stats(self):
        """Reset in-memory stats for a new cycle."""
        self.cycle_start_time = time.time()
        self.calls = defaultdict(int)
        self.response_times = []
        self.errors = 0
        self.total_calls = 0

    def _ensure_indexes(self):
        """Create indexes if they don't exist."""
        if not self.collection:
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

    def track_call(
        self,
        call_type: str,
        endpoint: str,
        success: bool = True,
        response_time_ms: float = 0.0
    ):
        """
        Track a single API call.

        Args:
            call_type: Type of call (see CALL_TYPES)
            endpoint: Specific endpoint/method called (e.g., "subreddit.new")
            success: Whether the call succeeded
            response_time_ms: Response time in milliseconds
        """
        self.calls[call_type] += 1
        self.total_calls += 1

        if response_time_ms > 0:
            self.response_times.append(response_time_ms)

        if not success:
            self.errors += 1

        logger.debug(f"Tracked API call: {call_type} ({endpoint}) - {response_time_ms:.1f}ms")

    def get_stats(self) -> dict:
        """
        Get current cycle statistics.

        Returns:
            dict with current cycle stats
        """
        elapsed = time.time() - self.cycle_start_time
        avg_response_time = (
            sum(self.response_times) / len(self.response_times)
            if self.response_times else 0.0
        )

        return {
            "subreddit": self.subreddit,
            "scraper_type": self.scraper_type,
            "total_calls": self.total_calls,
            "calls_by_type": dict(self.calls),
            "errors": self.errors,
            "avg_response_time_ms": round(avg_response_time, 2),
            "cycle_duration_seconds": round(elapsed, 2),
            "qpm": round(self.total_calls / (elapsed / 60), 2) if elapsed > 0 else 0
        }

    def flush_to_db(self, rate_limit_info: Optional[dict] = None, http_stats: Optional[dict] = None) -> bool:
        """
        Flush current cycle stats to MongoDB.

        Args:
            rate_limit_info: Rate limit snapshot from reddit.auth.limits
            http_stats: Actual HTTP request stats from CountingSession

        Returns:
            True if flush succeeded, False otherwise
        """
        if not self.collection:
            logger.debug("No MongoDB collection, skipping flush")
            return False

        # Allow flush even with 0 tracked calls if we have HTTP stats
        if self.total_calls == 0 and (http_stats is None or http_stats.get('cycle_requests', 0) == 0):
            logger.debug("No calls to flush")
            return True

        now = datetime.now(timezone.utc)

        # Calculate time buckets for aggregation
        hour_bucket = now.replace(minute=0, second=0, microsecond=0)
        day_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Calculate average response time
        avg_response_time = (
            sum(self.response_times) / len(self.response_times)
            if self.response_times else 0.0
        )

        # Build document
        doc = {
            "subreddit": self.subreddit,
            "scraper_type": self.scraper_type,
            "container_id": self.container_id,
            "timestamp": now,
            "hour_bucket": hour_bucket,
            "day_bucket": day_bucket,
            "calls": dict(self.calls),
            "total_calls": self.total_calls,
            "avg_response_time_ms": round(avg_response_time, 2),
            "errors": self.errors,
            "cycle_duration_seconds": round(time.time() - self.cycle_start_time, 2)
        }

        # Add rate limit info if available
        if rate_limit_info:
            doc["rate_limit"] = {
                "remaining": rate_limit_info.get("remaining"),
                "used": rate_limit_info.get("used"),
                "reset_in_seconds": rate_limit_info.get("reset_in_seconds")
            }

        # Add actual HTTP request counts and cost (critical for accurate billing)
        if http_stats:
            actual_requests = http_stats.get('cycle_requests', 0)
            cost_usd = http_stats.get('cycle_cost_usd', 0.0)
            doc["actual_http_requests"] = actual_requests
            doc["estimated_cost_usd"] = cost_usd
            # Calculate accuracy ratio (how much we undercount)
            if actual_requests > 0:
                doc["accuracy_ratio"] = round(self.total_calls / actual_requests, 4)
            else:
                doc["accuracy_ratio"] = 1.0

        try:
            self.collection.insert_one(doc)

            # Log with cost info if available
            if http_stats:
                logger.info(
                    f"Flushed API usage: {self.total_calls} tracked / "
                    f"{http_stats.get('cycle_requests', 0)} actual HTTP requests "
                    f"(${http_stats.get('cycle_cost_usd', 0):.4f})"
                )
            else:
                logger.info(
                    f"Flushed API usage: {self.total_calls} calls "
                    f"({self.errors} errors, {avg_response_time:.1f}ms avg)"
                )

            # Reset for next cycle
            self._reset_cycle_stats()
            return True

        except Exception as e:
            logger.error(f"Failed to flush API usage to MongoDB: {e}")
            return False


def track_api_call(tracker: Optional[APIUsageTracker], call_type: str, endpoint: str):
    """
    Context manager for tracking API calls with timing.

    Usage:
        with track_api_call(tracker, "posts_fetch", "subreddit.new"):
            posts = subreddit.new(limit=100)
    """
    class APICallContext:
        def __init__(self, tracker, call_type, endpoint):
            self.tracker = tracker
            self.call_type = call_type
            self.endpoint = endpoint
            self.start_time = None
            self.success = True

        def __enter__(self):
            self.start_time = time.time()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            elapsed_ms = (time.time() - self.start_time) * 1000
            self.success = exc_type is None

            if self.tracker:
                self.tracker.track_call(
                    self.call_type,
                    self.endpoint,
                    success=self.success,
                    response_time_ms=elapsed_ms
                )

            # Don't suppress exceptions
            return False

    return APICallContext(tracker, call_type, endpoint)


# Aggregation queries for API endpoints

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
                "total_calls": {"$sum": "$total_calls"},
                "total_errors": {"$sum": "$errors"},
                "avg_response_time": {"$avg": "$avg_response_time_ms"},
                "posts_fetch": {"$sum": "$calls.posts_fetch"},
                "comments_fetch": {"$sum": "$calls.comments_fetch"},
                "comments_expand": {"$sum": "$calls.comments_expand"},
                "metadata_fetch": {"$sum": "$calls.metadata_fetch"},
                "auth_check": {"$sum": "$calls.auth_check"},
                "rate_limit_check": {"$sum": "$calls.rate_limit_check"},
                # Actual HTTP request counts and cost
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

    # Get calls by subreddit (if not filtered)
    calls_by_subreddit = {}
    if not subreddit:
        pipeline = [
            {"$match": {"timestamp": {"$gte": today_start}}},
            {
                "$group": {
                    "_id": "$subreddit",
                    "total_calls": {"$sum": "$total_calls"}
                }
            },
            {"$sort": {"total_calls": -1}},
            {"$limit": 20}
        ]
        for doc in collection.aggregate(pipeline):
            calls_by_subreddit[doc["_id"]] = doc["total_calls"]

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
        "total_calls_today": today_stats.get("total_calls", 0),
        "total_calls_hour": hour_stats.get("total_calls", 0),
        "calls_by_type": {
            "posts_fetch": today_stats.get("posts_fetch", 0),
            "comments_fetch": today_stats.get("comments_fetch", 0),
            "comments_expand": today_stats.get("comments_expand", 0),
            "metadata_fetch": today_stats.get("metadata_fetch", 0),
            "auth_check": today_stats.get("auth_check", 0),
            "rate_limit_check": today_stats.get("rate_limit_check", 0),
        },
        "calls_by_subreddit": calls_by_subreddit,
        "avg_response_time_ms": round(today_stats.get("avg_response_time", 0), 2),
        "error_rate": round(
            today_stats.get("total_errors", 0) / today_stats.get("total_calls", 1),
            4
        ) if today_stats.get("total_calls", 0) > 0 else 0,
        "rate_limit": rate_limit,
        # Actual HTTP requests and cost (accurate billing data)
        "actual_http_requests_today": actual_requests_today,
        "actual_http_requests_hour": actual_requests_hour,
        "cost_usd_today": round(cost_today, 4),
        "cost_usd_hour": round(cost_hour, 4),
    }


def get_usage_trends(
    db,
    period: str = "day",
    granularity: str = "hour",
    subreddit: Optional[str] = None
) -> dict:
    """
    Get time-series usage data for charting.

    Args:
        db: MongoDB database instance
        period: "hour", "day", or "week"
        granularity: "minute", "hour", or "day"
        subreddit: Optional subreddit filter

    Returns:
        Time-series data
    """
    collection = db[API_USAGE_CONFIG["collection_name"]]

    now = datetime.now(timezone.utc)

    # Calculate period start
    if period == "hour":
        period_start = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
    elif period == "day":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # week
        period_start = datetime.fromtimestamp(time.time() - 7 * 24 * 3600, tz=timezone.utc)

    # Determine bucket field
    if granularity == "minute":
        # Group by minute (no pre-computed bucket, use $dateTrunc)
        date_group = {
            "$dateTrunc": {
                "date": "$timestamp",
                "unit": "minute"
            }
        }
    elif granularity == "hour":
        date_group = "$hour_bucket"
    else:  # day
        date_group = "$day_bucket"

    # Build match stage
    match_stage = {"timestamp": {"$gte": period_start}}
    if subreddit:
        match_stage["subreddit"] = subreddit

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": date_group,
                "calls": {"$sum": "$total_calls"},
                "errors": {"$sum": "$errors"},
                "avg_response_time": {"$avg": "$avg_response_time_ms"}
            }
        },
        {"$sort": {"_id": 1}}
    ]

    data = []
    for doc in collection.aggregate(pipeline):
        data.append({
            "timestamp": doc["_id"].isoformat() if doc["_id"] else None,
            "calls": doc["calls"],
            "errors": doc["errors"],
            "avg_response_time_ms": round(doc.get("avg_response_time", 0), 2)
        })

    return {
        "period": period,
        "granularity": granularity,
        "subreddit": subreddit,
        "data": data
    }
