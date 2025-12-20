#!/usr/bin/env python3
"""
HTTP Request Counter for Reddit API Cost Tracking

Wraps requests.Session to count every HTTP request made to Reddit's API.
This enables accurate cost calculation at $0.24 per 1,000 requests.

Usage:
    from http_request_counter import CountingSession

    http_session = CountingSession()
    reddit = praw.Reddit(
        ...,
        requestor_kwargs={'session': http_session}
    )

    # After scraping:
    print(f"Requests: {http_session.get_count()}")
    print(f"Cost: ${http_session.get_cost():.4f}")
"""

import requests
import time
import logging
from threading import Lock
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from config import LOGGING_CONFIG

# Configure logging
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True
)
logger = logging.getLogger("http-request-counter")


# Reddit API pricing (as of 2023, still in effect 2025)
COST_PER_1000_REQUESTS = 0.24


class CountingSession(requests.Session):
    """
    A requests.Session that counts every HTTP request for cost tracking.

    Intercepts all HTTP calls at the transport layer, ensuring we capture
    every request PRAW makes - including pagination, retries, and internal calls.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = Lock()
        self._request_count = 0
        self._cycle_request_count = 0  # Resets each cycle
        self._total_response_time_ms = 0.0
        self._error_count = 0
        self._requests_log: List[Dict[str, Any]] = []
        self._max_log_size = 10000  # Prevent memory bloat
        self._log_requests = True  # Can disable for performance

    def request(self, method, url, **kwargs):
        """
        Override request method to count and time every HTTP call.
        """
        start_time = time.time()
        status_code = None
        error = None

        try:
            response = super().request(method, url, **kwargs)
            status_code = response.status_code
            return response

        except Exception as e:
            error = str(e)
            with self._lock:
                self._error_count += 1
            raise

        finally:
            elapsed_ms = (time.time() - start_time) * 1000

            with self._lock:
                self._request_count += 1
                self._cycle_request_count += 1
                self._total_response_time_ms += elapsed_ms

                # Log request details if enabled
                if self._log_requests and len(self._requests_log) < self._max_log_size:
                    self._requests_log.append({
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'method': method,
                        'url': self._sanitize_url(url),
                        'status': status_code,
                        'elapsed_ms': round(elapsed_ms, 2),
                        'error': error
                    })

    def _sanitize_url(self, url: str) -> str:
        """Remove sensitive parts from URL for logging."""
        # Keep just the path, not query params (may contain tokens)
        if '?' in url:
            return url.split('?')[0]
        return url

    def get_count(self) -> int:
        """Get total request count since session creation."""
        with self._lock:
            return self._request_count

    def get_cycle_count(self) -> int:
        """Get request count for current cycle (since last reset)."""
        with self._lock:
            return self._cycle_request_count

    def get_cost(self) -> float:
        """Calculate total cost at $0.24 per 1,000 requests."""
        return (self.get_count() / 1000) * COST_PER_1000_REQUESTS

    def get_cycle_cost(self) -> float:
        """Calculate cost for current cycle."""
        return (self.get_cycle_count() / 1000) * COST_PER_1000_REQUESTS

    def get_avg_response_time(self) -> float:
        """Get average response time in milliseconds."""
        with self._lock:
            if self._request_count == 0:
                return 0.0
            return self._total_response_time_ms / self._request_count

    def get_error_count(self) -> int:
        """Get count of failed requests."""
        with self._lock:
            return self._error_count

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        with self._lock:
            return {
                'total_requests': self._request_count,
                'cycle_requests': self._cycle_request_count,
                'total_cost_usd': round((self._request_count / 1000) * COST_PER_1000_REQUESTS, 6),
                'cycle_cost_usd': round((self._cycle_request_count / 1000) * COST_PER_1000_REQUESTS, 6),
                'avg_response_time_ms': round(
                    self._total_response_time_ms / self._request_count if self._request_count > 0 else 0,
                    2
                ),
                'error_count': self._error_count,
                'error_rate': round(
                    self._error_count / self._request_count if self._request_count > 0 else 0,
                    4
                )
            }

    def get_recent_requests(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get most recent request logs."""
        with self._lock:
            return self._requests_log[-limit:]

    def reset_cycle(self) -> Dict[str, Any]:
        """
        Reset cycle counters and return the cycle stats.
        Called at end of each scrape cycle.
        """
        with self._lock:
            stats = {
                'cycle_requests': self._cycle_request_count,
                'cycle_cost_usd': round((self._cycle_request_count / 1000) * COST_PER_1000_REQUESTS, 6),
            }
            self._cycle_request_count = 0
            self._requests_log = []  # Clear log for next cycle
            return stats

    def reset_all(self) -> Dict[str, Any]:
        """
        Full reset - returns final stats and clears everything.
        Use sparingly, typically only on scraper restart.
        """
        with self._lock:
            stats = {
                'total_requests': self._request_count,
                'total_cost_usd': round((self._request_count / 1000) * COST_PER_1000_REQUESTS, 6),
                'error_count': self._error_count,
                'avg_response_time_ms': round(
                    self._total_response_time_ms / self._request_count if self._request_count > 0 else 0,
                    2
                )
            }
            self._request_count = 0
            self._cycle_request_count = 0
            self._total_response_time_ms = 0.0
            self._error_count = 0
            self._requests_log = []
            return stats


def create_counting_session() -> CountingSession:
    """Factory function to create a new CountingSession."""
    return CountingSession()


# Global instance for simple use cases (shared across module)
# For multi-scraper setups, create separate instances
_global_session: Optional[CountingSession] = None


def get_global_session() -> CountingSession:
    """Get or create the global counting session."""
    global _global_session
    if _global_session is None:
        _global_session = CountingSession()
    return _global_session
