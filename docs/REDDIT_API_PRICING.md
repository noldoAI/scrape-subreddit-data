# Reddit API Pricing & Cost Tracking

This document covers Reddit's official API pricing model, what counts as billable requests, and how our system tracks costs accurately.

## Official Pricing

**$0.24 per 1,000 API requests** for commercial applications.

- Announced: April 18, 2023
- Effective: July 1, 2023
- Ended Reddit's free API access (available since 2008)

## What Counts as a Billable Request

Reddit bills per **HTTP request** to `oauth.reddit.com`, NOT per high-level PRAW/library call.

### Billable

| Request Type | Description |
|--------------|-------------|
| Every HTTP call to oauth.reddit.com | Core API requests |
| Failed/error requests | Still billed even if they return errors |
| Token refresh requests | OAuth token renewal (every hour) |
| Pagination calls | Each page of 100 items is a separate request |
| Retry attempts | Each retry counts as a new request |
| Rate limit exceeded responses | Still billable despite returning no data |

### Not Billable

| Operation | Why Not Billed |
|-----------|----------------|
| Reading `reddit.auth.limits` | Reads cached response headers, no HTTP call |
| In-memory data access | Already fetched data |
| Client-side filtering | Happens after API response |
| Local processing | No network call |

## Rate Limits

### Free Tier (Non-Commercial)

| Access Type | Rate Limit | Notes |
|-------------|------------|-------|
| OAuth authenticated | 100 requests/minute | Per client ID |
| Unauthenticated | 10 requests/minute | Blocked in practice |

Rate limits are averaged over a **10-minute window** (600 requests/10 min).

### Rate Limit Headers

Reddit returns these headers with each response:

```
X-Ratelimit-Used: 45          # Requests used this period
X-Ratelimit-Remaining: 555    # Requests remaining
X-Ratelimit-Reset: 234        # Seconds until reset
```

## PRAW to HTTP Request Mapping

PRAW makes many more HTTP requests than visible API calls. Understanding this is critical for accurate cost estimation.

### Request Multiplication Examples

| PRAW Call | Actual HTTP Requests | Why |
|-----------|---------------------|-----|
| `subreddit.hot(limit=100)` | 1-2 | Single page, possible retry |
| `subreddit.hot(limit=250)` | 3 | 100 + 100 + 50 (pagination) |
| `subreddit.hot(limit=1000)` | 10 | 10 pages of 100 |
| `post.comments.list()` | 1-5+ | Depends on comment count |
| `replace_more(limit=None)` | 0-100+ | Expands all "More Comments" |
| `replace_more(limit=0)` | 0 | Skips expansion |
| Lazy attribute access | 1 per object | Hidden API calls |

### Pagination Math

Reddit returns maximum **100 items per request**:

```
requested_items / 100 = number_of_requests (rounded up)

Examples:
- 100 items = 1 request
- 101 items = 2 requests
- 250 items = 3 requests
- 1000 items = 10 requests
```

### Why This Matters

Without transport-layer counting, costs would be **underestimated by 2-5x**.

```
Code shows:        1 call to subreddit.hot(limit=500)
Actual HTTP:       5 requests (5 pages of 100)
Billed cost:       5x what code suggests
```

## Cost Examples

### Monthly Cost Estimates

| Use Case | Monthly Requests | Monthly Cost |
|----------|------------------|--------------|
| 10 subreddits, hourly check | 8,640 | $2.07 |
| 100 subreddits, hourly check | 86,400 | $20.74 |
| Active scraping bot | 500,000+ | $120+ |
| Moderate commercial use | 5 million | $1,200 |
| Heavy commercial use | 50 million | $12,000 |

### Per-Scraper Estimates (This System)

With default configuration:

| Component | Requests/Cycle | Notes |
|-----------|---------------|-------|
| Post scraping (3 sorts) | ~9 | 3 methods × ~3 pages each |
| Comment scraping | ~12 | 6 posts × 2 calls each |
| Metadata update | ~5 | Rules, sample posts, etc. |
| **Total per cycle** | ~26 | Per subreddit |

At 1-minute intervals: **~26 requests/minute** per subreddit.

### Scaling Estimates

| Subreddits | Requests/Hour | Daily Cost | Monthly Cost |
|------------|---------------|------------|--------------|
| 1 | 1,560 | $0.37 | $11.23 |
| 5 | 7,800 | $1.87 | $56.16 |
| 10 | 15,600 | $3.74 | $112.32 |
| 30 | 46,800 | $11.23 | $336.96 |

## How We Track Costs

### Architecture

```
PRAW API calls
    ↓
prawcore.Session
    ↓
prawcore.Requestor
    ↓
requests.Session  ← CountingSession intercepts here
    ↓
HTTP to oauth.reddit.com
```

### CountingSession

We wrap `requests.Session` to intercept every HTTP call:

```python
# tracking/http_request_counter.py
class CountingSession(requests.Session):
    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        self._request_count += 1  # Count actual HTTP request
        return response
```

### Integration with PRAW

```python
from tracking import CountingSession

http_session = CountingSession()
reddit = praw.Reddit(
    client_id=...,
    client_secret=...,
    requestor_kwargs={'session': http_session}  # Inject counter
)

# After scraping:
print(f"Requests: {http_session.get_count()}")
print(f"Cost: ${http_session.get_cost():.4f}")
```

### MongoDB Storage

Usage data is stored in `reddit_api_usage` collection:

```javascript
{
  "subreddit": "wallstreetbets",
  "scraper_type": "posts",
  "timestamp": ISODate("2025-01-20T15:30:00Z"),
  "actual_http_requests": 156,
  "estimated_cost_usd": 0.0374,
  "cycle_duration_seconds": 45.2,
  "rate_limit": {
    "remaining": 450,
    "used": 150,
    "reset_in_seconds": 234
  }
}
```

## Dashboard Cost Panel

The web dashboard displays:

| Metric | Description |
|--------|-------------|
| **Today** | Cumulative cost + requests since midnight |
| **Last Hour** | Cost + requests in the last 60 minutes |
| **Avg/Hour** | Today's total ÷ hours elapsed since first request |
| **Avg/Day** | Historical average (last 7 days) |
| **Monthly** | Projected monthly cost (avg/day × 30) |
| **Per-Subreddit** | Breakdown table by subreddit |

## API Endpoints

### Get Cost Stats

```bash
GET /api/usage/cost
```

Response:
```json
{
  "status": "ok",
  "pricing": {
    "cost_per_1000_requests": 0.24,
    "currency": "USD"
  },
  "today": {
    "requests": 45230,
    "cost_usd": 10.8552,
    "posts_scraped": 12450,
    "comments_scraped": 89340
  },
  "last_hour": {
    "requests": 1850,
    "cost_usd": 0.444
  },
  "averages": {
    "hourly_requests": 1900,
    "hourly_cost_usd": 0.456,
    "daily_requests": 45600,
    "daily_cost_usd": 10.944,
    "days_of_data": 7
  },
  "projections": {
    "monthly_requests": 1368000,
    "monthly_cost_usd": 328.32
  },
  "by_subreddit": {
    "wallstreetbets": {"requests": 12500, "cost_usd": 3.0},
    "stocks": {"requests": 8900, "cost_usd": 2.136}
  }
}
```

### Get Usage Trends

```bash
GET /api/usage/trends?hours=24
```

Returns hourly breakdown for charting.

## Best Practices for Cost Optimization

### 1. Reduce Pagination

```python
# Bad: 10 HTTP requests
posts = subreddit.hot(limit=1000)

# Better: 1 HTTP request
posts = subreddit.hot(limit=100)
```

### 2. Skip Comment Expansion

```python
# Bad: Potentially 100+ requests
submission.comments.replace_more(limit=None)

# Good: 0 additional requests
submission.comments.replace_more(limit=0)
```

### 3. Use Depth Limiting

```python
# Only fetch top 3 levels of comments
# Captures 85-90% of valuable discussion
max_comment_depth = 3
```

### 4. Batch Subreddits

```python
# Instead of 30 separate scrapers:
# Use multi-subreddit rotation with one account
subreddits = ["stocks", "investing", "wallstreetbets", ...]
```

### 5. Increase Intervals

```python
# 60 seconds = 26 requests/minute/subreddit
# 300 seconds = 5.2 requests/minute/subreddit (5x cheaper)
interval = 300
```

## Sources

- [Reddit API Pricing Guide (rankvise.com)](https://rankvise.com/blog/reddit-api-cost-guide/) - Comprehensive pricing breakdown
- [PRAW Rate Limits Documentation](https://praw.readthedocs.io/en/stable/getting_started/ratelimits.html) - Official PRAW docs
- [Reddit API Limits (data365.co)](https://data365.co/blog/reddit-api-limits) - Rate limit details
- [Reddit API Pricing Explained (tripleareview.com)](https://tripleareview.com/reddit-api-pricing-explained/) - Billing model
- [Reddit Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) - Official Reddit documentation
- [Reddit API Guide (apidog.com)](https://apidog.com/blog/reddit-api-guide/) - API features and setup

## Related Documentation

- [API_SETUP.md](API_SETUP.md) - Setting up Reddit API credentials
- [SETUP.md](SETUP.md) - System installation and configuration
- [../CLAUDE.md](../CLAUDE.md) - Full project documentation
