#!/usr/bin/env python3
"""
LLM Enrichment Service for Subreddit Metadata

Uses Azure GPT-4o-mini to analyze subreddit metadata and generate
structured audience profiles for persona-based search.

Usage:
    from llm_enrichment import SubredditEnricher
    enricher = SubredditEnricher()
    result = enricher.enrich_subreddit(metadata)
"""

import os
import json
import logging
from datetime import datetime, UTC
from typing import Dict, Optional

from dotenv import load_dotenv
from openai import AzureOpenAI

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('llm-enrichment')

# Load environment variables
load_dotenv()


class SubredditEnricher:
    """
    Service for enriching subreddit metadata with LLM-generated audience profiles.

    Uses Azure GPT-4o-mini to analyze subreddit content and extract:
    - audience_profile: One sentence describing who uses this subreddit
    - audience_types: List of user types (founders, developers, etc.)
    - user_intents: Why users come here (seeking advice, sharing projects, etc.)
    - pain_points: Problems users discuss
    - content_themes: Common discussion themes
    """

    def __init__(self):
        """Initialize Azure OpenAI client."""
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.deployment = os.getenv("AZURE_DEPLOYMENT_NAME", "gpt-4o-mini")

        if not self.endpoint or not self.api_key:
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set"
            )

        self.client = AzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version="2024-02-01"
        )

        logger.info(f"Initialized Azure OpenAI client (deployment: {self.deployment})")

    def enrich_subreddit(self, metadata: Dict) -> Optional[Dict]:
        """
        Generate audience profile from subreddit metadata.

        Args:
            metadata: Subreddit metadata dict with fields:
                - subreddit_name
                - title
                - public_description
                - sample_posts_titles
                - rules_text

        Returns:
            Dict with enrichment data or None on error
        """
        subreddit_name = metadata.get('subreddit_name', 'unknown')

        try:
            # Build prompt with available data
            prompt = self._build_prompt(metadata)

            # Call Azure OpenAI
            response = self.client.chat.completions.create(
                model=self.deployment,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert at analyzing online communities. Extract structured audience information from subreddit data. Always respond with valid JSON."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.3,  # Lower temperature for consistent output
                max_tokens=500
            )

            # Parse response
            content = response.choices[0].message.content
            enrichment = json.loads(content)

            # Add metadata
            enrichment["generated_at"] = datetime.now(UTC)
            enrichment["model"] = self.deployment

            logger.info(f"Enriched r/{subreddit_name}: {enrichment.get('audience_profile', '')[:50]}...")

            return enrichment

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response for r/{subreddit_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error enriching r/{subreddit_name}: {e}")
            return None

    def _build_prompt(self, metadata: Dict) -> str:
        """Build the prompt for LLM enrichment."""
        subreddit_name = metadata.get('subreddit_name', 'unknown')
        title = metadata.get('title', '')
        description = metadata.get('public_description', '')
        sample_posts = metadata.get('sample_posts_titles', '')[:600]
        rules = metadata.get('rules_text', '')[:400]

        # Also include sample post excerpts if available
        sample_excerpts = ""
        if metadata.get('sample_posts'):
            excerpts = [
                p.get('selftext_excerpt', '')[:100]
                for p in metadata.get('sample_posts', [])[:5]
                if p.get('selftext_excerpt')
            ]
            if excerpts:
                sample_excerpts = " | ".join(excerpts)

        prompt = f"""Analyze this subreddit and extract audience information.

Subreddit: r/{subreddit_name}
Title: {title}
Description: {description}
Sample post titles: {sample_posts}
Sample post content: {sample_excerpts}
Rules: {rules}

Based on this information, identify:
1. Who uses this subreddit (the target audience)
2. What types of users frequent it
3. What they come here to do
4. What problems/pain points they discuss
5. Common content themes

Return a JSON object with these fields:
{{
  "audience_profile": "A single sentence describing who uses this subreddit and why",
  "audience_types": ["list", "of", "user", "types"],
  "user_intents": ["what", "users", "come", "here", "to", "do"],
  "pain_points": ["problems", "users", "discuss"],
  "content_themes": ["common", "discussion", "themes"]
}}

Keep each list to 3-6 items. Be specific and actionable."""

        return prompt

    def enrich_batch(self, metadata_list: list, delay: float = 0.5) -> list:
        """
        Enrich multiple subreddits.

        Args:
            metadata_list: List of subreddit metadata dicts
            delay: Delay between API calls in seconds

        Returns:
            List of (subreddit_name, enrichment) tuples
        """
        import time

        results = []
        total = len(metadata_list)

        for i, metadata in enumerate(metadata_list, 1):
            subreddit_name = metadata.get('subreddit_name', 'unknown')
            logger.info(f"[{i}/{total}] Enriching r/{subreddit_name}...")

            enrichment = self.enrich_subreddit(metadata)
            results.append((subreddit_name, enrichment))

            if i < total:
                time.sleep(delay)

        successful = sum(1 for _, e in results if e is not None)
        logger.info(f"Batch complete: {successful}/{total} successful")

        return results


def test_enrichment():
    """Test the enrichment service with a sample subreddit."""
    # Sample metadata for testing
    sample_metadata = {
        "subreddit_name": "SaaS",
        "title": "Software As a Service Companies",
        "public_description": "Pair your front-end with a database and an AI backbone using SaaS.",
        "sample_posts_titles": "Just launched my MVP and got 100 users | How do you handle pricing for enterprise? | Feedback on my landing page | What's your CAC to LTV ratio? | Best tools for customer success",
        "rules_text": "No spam | Self-promo Saturday only | Be constructive in feedback | No job postings",
        "sample_posts": [
            {"selftext_excerpt": "I've been working on this for 6 months and finally launched. Looking for feedback from other founders..."},
            {"selftext_excerpt": "We're seeing high churn in the first month. What strategies have worked for you to improve retention?"}
        ]
    }

    enricher = SubredditEnricher()
    result = enricher.enrich_subreddit(sample_metadata)

    if result:
        print("\n" + "="*60)
        print("ENRICHMENT RESULT")
        print("="*60)
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Enrichment failed!")


if __name__ == "__main__":
    test_enrichment()
