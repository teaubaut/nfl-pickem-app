# NFL Pool Picks (Automated Edge)

A zero-cost, automated personal tool designed to give you a mathematical edge in weekly NFL straight-up (win/loss) pick'em pools. 

Instead of relying on gut feelings or basic ESPN power rankings, this tool aggregates **vig-free sportsbook moneylines**, **prediction market pricing**, and **public ticket percentages** to identify high-leverage picks and optimal Monday Night tiebreakers. It runs entirely on GitHub Actions and deploys as a mobile-first Progressive Web App (PWA) to GitHub Pages.

## Key Features

* **Blended Probability Engine:** Averages vig-free implied probabilities from US sportsbooks (The Odds API) with real-time prediction market prices (Kalshi).
* **Public Leverage Detection:** Scrapes public moneyline ticket percentages (Action Network) to flag games where the public is blindly over-valuing a favorite or under-valuing a live underdog.
* **Smart MNF Tiebreaker:** Calculates a projected Monday Night Football score based on the spread and total, then mathematically snaps the projection to the closest key NFL scoring numbers (3, 7, 10, 13, etc.) to prevent impossible margins.
* **Mobile-First PWA:** Designed for iOS. Save it to your Home Screen for a native app experience, complete with pull-to-refresh, dark mode, and a one-tap "Copy Picks" clipboard button.
* **100% Serverless & Free:** Uses zero third-party Python dependencies. Scheduled via GitHub Actions and hosted statically on GitHub Pages.

## Architecture

1. **`fetch_data.py`**: A pure Python (Standard Library) scraper. 
    * Pulls the weekly schedule from ESPN.
    * Fetches consensus odds from The Odds API.
    * Pulls event contracts from the Kalshi API.
    * Scrapes public consensus from Action Network.
    * Outputs a compiled `data.json` file.
2. **`index.html`**: A single-file frontend built with Tailwind CSS (via CDN) that reads `data.json` and renders the mobile UI.
3. **`.github/workflows/update_odds.yml`**: A GitHub Action that runs automatically on Thursdays and Sundays (or via manual dispatch) on a `macos-latest` runner (to bypass ESPN anti-bot IP blocks), commits the fresh JSON data, and triggers a web deployment.

## Setup & Installation

### 1. Repository Configuration
1. Clone or fork this repository.
2. Go to your repository **Settings > Pages**. Under **Build and deployment**, set the source to deploy from the `main` branch.

### 2. API Keys
You will need a free API key from [The Odds API](https://the-odds-api.com/). 
1. Go to your repository **Settings > Secrets and variables > Actions**.
2. Click **New repository secret**.
3. Name it `ODDS_API_KEY` and paste your key into the secret field.

### 3. First Run
1. Go to the **Actions** tab in your repository.
2. Select **Update NFL Picks Data** on the left.
3. Click **Run workflow**. 
4. Once the job completes successfully, your site will be live at `https://[your-username].github.io/[repo-name]/`.

## iOS Home Screen Installation

To install this as a native-feeling app on your iPhone:
1. Open your live GitHub Pages URL in **Safari**.
2. Tap the **Share** icon at the bottom of the screen.
3. Scroll down and tap **Add to Home Screen**.
4. Tap **Add**. You can now launch the app directly from your home screen without browser toolbars.

## Local Development

If you want to test the scraper or UI on your local machine, no `pip install` is required. 

```bash
# 1. Export your API key
export ODDS_API_KEY="your_api_key_here"

# 2. Run the scraper to generate data.json
python3 fetch_data.py

# 3. Spin up a local web server to view the frontend
python3 -m http.server

# 4. Then open ⁠http://localhost:8000⁠ in your browser.
