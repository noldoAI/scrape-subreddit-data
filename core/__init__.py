#!/usr/bin/env python3
"""
Core utilities for the Reddit Scraper system.

Contains shared modules:
- rate_limits: Reddit API rate limiting
- azure_logging: Azure Application Insights logging
- metrics: Prometheus metrics
"""

from core.rate_limits import check_rate_limit
from core.azure_logging import setup_azure_logging
from core.metrics import update_metrics_from_db, get_metrics, init_metrics
