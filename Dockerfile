FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy scraper-specific requirements (no PyTorch needed)
COPY requirements-scraper.txt .

# Install Python dependencies (lightweight - no ML packages)
RUN pip install --no-cache-dir -r requirements-scraper.txt

# Copy application files
COPY posts_scraper.py .
COPY comments_scraper.py .
COPY config.py .
COPY rate_limits.py .
COPY metrics.py .
COPY .env* ./

# Expose Prometheus metrics port
EXPOSE 9100

# Change ownership to non-root user
RUN chown -R appuser:appuser /app
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import posts_scraper; print('OK')" || exit 1

# Default command - scrape posts from wallstreetbets
CMD ["python", "posts_scraper.py", "wallstreetbets"]
