#!/usr/bin/env python3
"""
API usage tracking for the Reddit Scraper system.

Contains modules for counting and costing Reddit API requests:
- http_request_counter: CountingSession for accurate HTTP request counting
- api_usage_tracker: MongoDB storage and aggregation for usage stats
"""

from tracking.http_request_counter import CountingSession, COST_PER_1000_REQUESTS
from tracking.api_usage_tracker import APIUsageTracker, track_api_call, get_usage_stats, get_usage_trends
