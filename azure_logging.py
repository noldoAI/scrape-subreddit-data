#!/usr/bin/env python3
"""
Azure Application Insights logging helper.
Configures loggers to send WARNING+ logs to Azure while keeping console output.
"""

import logging
import os

def setup_azure_logging(logger_name: str, level=logging.INFO) -> logging.Logger:
    """
    Configure logger with Azure Application Insights.

    Args:
        logger_name: Name for the logger
        level: Minimum log level (default: INFO)

    Returns:
        Configured logger instance

    Environment:
        APPLICATIONINSIGHTS_CONNECTION_STRING: Azure connection string (optional)
        If not set, only console logging is enabled.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    # Console handler (keep existing behavior)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(console)

    # Azure handler (if connection string provided)
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn_str:
        try:
            from opencensus.ext.azure.log_exporter import AzureLogHandler

            azure_handler = AzureLogHandler(connection_string=conn_str)
            azure_handler.setLevel(logging.WARNING)  # Only send WARNING+ to Azure
            azure_handler.setFormatter(logging.Formatter(
                '%(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(azure_handler)
            logger.info(f"Azure Application Insights enabled for logger: {logger_name}")
        except ImportError:
            logger.warning("opencensus-ext-azure not installed, Azure logging disabled")
        except Exception as e:
            logger.warning(f"Failed to setup Azure logging: {e}")

    return logger
