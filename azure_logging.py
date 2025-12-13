#!/usr/bin/env python3
"""
Azure Application Insights logging via OpenTelemetry.
Replaces deprecated opencensus-ext-azure (deprecated Sept 2024).

Configures loggers to send WARNING+ logs to Azure while keeping console output.
"""

import logging
import os

_azure_configured = False

def setup_azure_logging(logger_name: str, level=logging.INFO) -> logging.Logger:
    """
    Configure logger with Azure Application Insights (OpenTelemetry).

    Args:
        logger_name: Name for the logger (used to filter in Azure)
        level: Minimum log level (default: INFO)

    Returns:
        Configured logger instance

    Environment:
        APPLICATIONINSIGHTS_CONNECTION_STRING: Azure connection string (optional)
        If not set, only console logging is enabled.
    """
    global _azure_configured

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    # Console handler (always enabled)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(console)

    # Azure OpenTelemetry (configure once globally)
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn_str and not _azure_configured:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME

            # Set cloud role name (for filtering in Azure Portal)
            # Can be overridden via OTEL_SERVICE_NAME env var
            service_name = os.getenv("OTEL_SERVICE_NAME", "reddit-scraper")
            resource = Resource.create({SERVICE_NAME: service_name})

            # Configure Azure Monitor with the logger name and service name
            configure_azure_monitor(
                connection_string=conn_str,
                logger_name=logger_name,
                resource=resource,
            )
            _azure_configured = True
            logger.info(f"Azure Application Insights enabled (OpenTelemetry) - cloud_RoleName: {service_name}")
        except ImportError:
            logger.warning("azure-monitor-opentelemetry not installed, Azure logging disabled")
        except Exception as e:
            logger.warning(f"Failed to setup Azure logging: {e}")

    return logger
