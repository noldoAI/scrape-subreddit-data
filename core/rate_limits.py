import time
import logging

# Import centralized configuration
from config import LOGGING_CONFIG

# Configure logging with timestamps
logging.basicConfig(
    format=LOGGING_CONFIG["format"],
    datefmt=LOGGING_CONFIG["date_format"],
    level=getattr(logging, LOGGING_CONFIG["level"]),
    force=True  # Override any existing logging configuration
)
logger = logging.getLogger("rate-limits")


def check_rate_limit(reddit, min_remaining=50):
    """
    Check Reddit API rate limits and wait if necessary.
    
    Args:
        reddit: PRAW Reddit instance
        min_remaining (int): Minimum number of requests to keep in reserve
    
    Returns:
        dict: Rate limit information
    """
    try:
        rate_limit = reddit.auth.limits
        remaining = rate_limit.get('remaining')
        used = rate_limit.get('used')
        reset_time = rate_limit.get('reset_timestamp')
        
        # Handle None values
        if remaining is None or used is None or reset_time is None:
            logger.info("Rate limit info not available, adding precautionary delay...")
            time.sleep(1)
            return None
            
        time_until_reset = reset_time - time.time()
        
        logger.info(f"Rate limit - Remaining: {remaining}, Used: {used}, Reset in: {time_until_reset:.1f}s")
        
        # If we're running low on requests, wait for reset
        if remaining <= min_remaining:
            if time_until_reset > 0:
                logger.info(f"Rate limit low ({remaining} remaining). Waiting {time_until_reset:.1f} seconds for reset...")
                time.sleep(time_until_reset + 5)  # Add 5 seconds buffer
                logger.info("Rate limit reset. Continuing...")
            else:
                logger.info("Rate limit reset time has passed. Continuing...")
        
        return {
            'remaining': remaining,
            'used': used,
            'reset_in_seconds': time_until_reset
        }
    except Exception as e:
        logger.error(f"Error checking rate limit: {e}")
        # If we can't check rate limits, add a small delay as precaution
        time.sleep(1)
        return None
