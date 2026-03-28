# Pain Point Analysis Pipeline

General-purpose pipeline for discovering real-world pain points from any set of Reddit communities. Give it subreddits, it scrapes posts + comments, Claude Code reads everything and extracts what people struggle with.

Works for any domain — agriculture, woodworking, trucking, parenting, whatever. The subreddit list defines the scope.

## Flow

```
1. YOU                    Pick subreddits + domain focus
                               │
2. top_scraper.py         Fetch top posts + comment trees from Reddit API
                               │
3. top_data/*.json        Raw data: posts, comments, scores, authors
                               │
4. Claude Code            Read all JSON, filter noise, extract pain points
                               │
5. Ranked report          Problems, quotes, frequency, solution angles
```

## Steps

### 1. Pick Subreddits

Choose communities where practitioners discuss real problems — not news or memes. Good signals: complaint threads, "help me" posts, equipment discussions, "what do you wish existed" threads.

Pick 6-15 subreddits for good coverage. Examples:

**Agriculture**: farming, ranching, homesteading, gardening, Permaculture, BackyardChickens, beekeeping, livestock, vegetablegardening, smallfarm

**Trades**: electricians, plumbing, HVAC, Construction, carpentry

**Small business**: smallbusiness, Entrepreneur, ecommerce, freelance

### 2. Scrape

Run `top_scraper.py` once per subreddit. Each run fetches top posts + nested comment trees and saves to `top_data/{subreddit}_top_{time_filter}.json`.

```bash
source venv/bin/activate
python top_scraper.py <subreddit> --time-filter month --limit 50 --max-depth 3
```

Parameters:
- `--time-filter`: `hour`, `day`, `week`, `month`, `year`, `all`
- `--limit`: number of top posts (default 30)
- `--max-depth`: comment nesting depth (default 3)

### 3. Analyze

Give Claude Code all the JSON files and a prompt describing:
- **What domain** you're investigating
- **What counts as a pain point** in that domain (physical tasks, cost complaints, time sinks, danger, frustration)
- **What to ignore** (off-topic noise specific to those subreddits)
- **Output format** you want

Claude Code reads every post and every comment (including nested replies), filters out noise, groups similar complaints, counts unique users, and pulls direct quotes.

### 4. Output

For each pain point:
- **Problem**: one-sentence description
- **Mentions**: count of separate users describing this
- **Quotes**: 2-3 direct user quotes with subreddit source
- **Pain type**: dangerous / time-wasting / expensive / error-prone / physically hard
- **Solution angle**: what kind of product, service, tool, or process change could address this

Ranked by frequency. Only problems with 2+ separate users included.

## Tips for Good Results

| Factor | Good | Bad |
|--------|------|-----|
| Subreddit type | Practitioner communities (r/farming, r/ranching) | News aggregators (r/technology) |
| Time filter | `month` or `year` for volume | `week` can be too thin |
| Post limit | 50-100 for solid coverage | 10-20 misses long-tail problems |
| Comment depth | 3 captures most discussion | 1 misses where real complaints live (in replies) |
| Subreddit count | 6-15 for cross-community patterns | 1-2 gives narrow view |
| Analysis prompt | Specific about what to look for and ignore | Vague "find problems" gets noisy results |

## Limitations

- Only captures top posts — niche pain points in low-voted posts are missed
- `replace_more(limit=0)` skips collapsed comment threads (speed tradeoff)
- One Reddit account = rate limited; many subreddits in parallel may slow down
- Seasonal bias: scraping in March captures spring problems, misses harvest-season issues
- Pain points need human judgment to separate real problems from venting
- Analysis quality scales with data volume — more posts = better signal
