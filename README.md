# ACD Spatial Radar

A self-updating news radar for Advanced Cell Diagnostics / Bio-Techne Spatial field sales.
A GitHub Actions job (run manually from the Actions tab) pulls RSS feeds, PubMed, and NIH RePORTER,
scores each new item on two axes (relevance and importance) via the Claude API, applies a recency decay in code, and writes `data.json`.
GitHub Pages serves the dashboard, which reads that file. The job can also email you a digest of the run's new high-signal items.

No server to run. No hosting bill. The only cost is a few cents of Claude API usage per run.

## What is in here

| File | Job |
|------|-----|
| `sources.json` | The feed list and search terms. Edit this to add or tune sources. |
| `scripts/fetch_and_score.py` | Pulls sources, dedupes, scores via Claude, writes `data.json`, and emails the digest. |
| `scoring_config.json` | Tuning knobs: per-category score thresholds, the recency decay, and the digest settings. Edit and commit, no code changes needed. |
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

It runs only when you trigger it manually (Actions tab > Run workflow). To re-enable a schedule later, add a `schedule:` block back to `fetch.yml`.

## Email digest (optional)

The job can email you the run's new high-signal items. It sends only when something new clears the digest bar, so a frequent schedule will not spam you, and a quiet run sends nothing.

1. **Sign up at [resend.com](https://resend.com)** using the exact address you want the digest delivered to. Until you verify your own domain, the default `onboarding@resend.dev` sender can only deliver to that signup address. Create an API key.

2. **Add two secrets** (Settings > Secrets and variables > Actions):
   - `RESEND_API_KEY`: your Resend key.
   - `DIGEST_TO`: your email (the same one you signed up with).
   - Optional `DASHBOARD_URL`: your Pages URL, so the email footer links back to the radar.

3. **Tune it** in the `digest` block of `scoring_config.json`: `min_score` (the bar an item must clear to be emailed), `max_items`, `subject_prefix`, and `enabled` (set to `false` to pause email). To send to other people later, verify a domain in Resend and change the `from` line.

If the secrets are not set, the job still runs and just skips the email.

## Tuning

- **Score thresholds:** edit `scoring_config.json`. Each category has its own bar in `min_score` (an item is shown only if its 0-100 score clears the bar for its category), so competitor moves sit low and research sits high. The `recency_decay` block controls how fast older items fade. If the file is missing or malformed, the script logs a warning and falls back to built-in defaults rather than failing.
- **How scoring works:** the Claude call returns two numbers, `relevance` and `importance` (0-10 each). The script computes `score = relevance x importance x recency factor`, so an item must be both relevant and important to rank high. Recency is arithmetic in code, not a model guess.
- **Competitors tracked:** Molecular Instruments, 10x Genomics, Bruker/NanoString, Vizgen, Akoya/Quanterix, Navinci, plus adjacent imaging vendors. Edit the competitor block in the scoring prompt to add or remove.

- **Add or change sources:** edit `sources.json`. RSS entries just need a `name`, `url`, and `category_hint`. A broken feed is logged and skipped, it will not break the run.
- **Change what counts as relevant:** edit the `SCORING_SYSTEM` text in `fetch_and_score.py`. That prompt is the brain.
- **Change categories:** edit `CATEGORIES` in the script and the matching filter chips in `index.html`.

## Notes and honest caveats

- **Competitor feeds are unverified.** The 10x, Bruker, and Bio-Techne RSS URLs in `sources.json` are best guesses. Run the job once and check the Actions log: any that 404 will say so. If one has no real feed, options are to find its investor-relations RSS, or drop it and rely on PubMed/NIH for that competitor's research footprint.
- **NIH RePORTER is the high-value source.** Newly funded spatial/ISH grants name the PI and institution, which is your direct territory signal. It needs no key.
- **API key ownership.** If this is Bio-Techne work product, prefer a company-managed repo and a company Anthropic key over your personal one, and give IT/Legal a heads up. Worth sorting before this becomes a team tool.
- **Local preview:** open the folder with a simple web server (`python -m http.server`) rather than double-clicking `index.html`, otherwise the browser blocks the `data.json` fetch.

No em dashes anywhere in this project, per standing preference.
