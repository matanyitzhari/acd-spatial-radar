# ACD Spatial Radar

A self-updating news radar for Advanced Cell Diagnostics / Bio-Techne Spatial field sales.
A scheduled GitHub Actions job pulls RSS feeds, PubMed, and NIH RePORTER every 6 hours,
scores each new item for relevance via the Claude API, and writes `data.json`.
GitHub Pages serves the dashboard, which reads that file.

No server to run. No hosting bill. The only cost is a few cents of Claude API usage per run.

## What is in here

| File | Job |
|------|-----|
| `sources.json` | The feed list and search terms. Edit this to add or tune sources. |
| `scripts/fetch_and_score.py` | Pulls sources, dedupes, scores via Claude, writes `data.json`. |
| `.github/workflows/fetch.yml` | The cron scheduler that runs the script and commits results. |
| `index.html` | The dashboard. Reads `data.json`. |
| `data.json` | The output. Auto-committed by the job. A sample is included so the dashboard renders before the first real run. |

## One-time setup

1. **Create a new GitHub repository** (you must do this yourself, I cannot create accounts or repos for you). Name it something like `acd-spatial-radar`. Upload all these files preserving the folder structure.

2. **Add your Claude API key as a secret.**
   In the repo: Settings > Secrets and variables > Actions > New repository secret.
   - Name: `ANTHROPIC_API_KEY`
   - Value: your key
   The key is never written into the code or `data.json`. See the note on company keys below.

3. **Enable GitHub Pages.**
   Settings > Pages > Build and deployment > Source: "Deploy from a branch", branch `main`, folder `/ (root)`. Save.
   Your dashboard will be live at `https://<your-username>.github.io/acd-spatial-radar/`.

4. **Run it once manually** to populate real data.
   Actions tab > "Fetch and score spatial news" > Run workflow.
   When it finishes it will have updated `data.json` and the dashboard will show real items.

After that it runs itself every 6 hours. Adjust the cadence by editing the `cron` line in `fetch.yml`.

## Tuning

- **Add or change sources:** edit `sources.json`. RSS entries just need a `name`, `url`, and `category_hint`. A broken feed is logged and skipped, it will not break the run.
- **Change what counts as relevant:** edit the `SCORING_SYSTEM` text in `fetch_and_score.py`. That prompt is the brain.
- **Change categories:** edit `CATEGORIES` in the script and the matching filter chips in `index.html`.

## Notes and honest caveats

- **Competitor feeds are unverified.** The 10x, Bruker, and Bio-Techne RSS URLs in `sources.json` are best guesses. Run the job once and check the Actions log: any that 404 will say so. If one has no real feed, options are to find its investor-relations RSS, or drop it and rely on PubMed/NIH for that competitor's research footprint.
- **NIH RePORTER is the high-value source.** Newly funded spatial/ISH grants name the PI and institution, which is your direct territory signal. It needs no key.
- **API key ownership.** If this is Bio-Techne work product, prefer a company-managed repo and a company Anthropic key over your personal one, and give IT/Legal a heads up. Worth sorting before this becomes a team tool.
- **Local preview:** open the folder with a simple web server (`python -m http.server`) rather than double-clicking `index.html`, otherwise the browser blocks the `data.json` fetch.

No em dashes anywhere in this project, per standing preference.
