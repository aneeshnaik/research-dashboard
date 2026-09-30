# Research Dashboard

A nightly GitHub Action reads the "Research Tasks" GitHub Project and publishes a
one-page dashboard: headline numbers, upcoming work by research project, and
points completed per day, per week and per week by project.

Files:

```
build_dashboard.py                     the script (settings block at the top)
requirements.txt                       Python packages it needs
.github/workflows/build-dashboard.yml  runs the script daily and publishes the page
```

## Setup (about 15 minutes)

### 1. Create a repository for the dashboard
Make a new repository, e.g. `research-dashboard`, separate from `research-admin`.
Upload the three files, keeping the `.github/workflows/` folder path.

**Public or private?** GitHub Pages on a free account needs a *public* repository,
and the published page is public either way (anyone with the URL can see it;
it's marked `noindex` so search engines skip it). The page shows only aggregate
numbers and your research project names, never issue titles. If that's too
much, make the repo private and use the downloadable copy instead (step 5, option B).

### 2. Fill in the settings block
Open `build_dashboard.py` and edit the block at the top:

- `OWNER_LOGIN`: your GitHub username (or organisation name).
- `OWNER_TYPE`: `"user"` or `"organization"`.
- `PROJECT_NUMBER`: the number at the end of the project URL,
  e.g. `github.com/users/NAME/projects/3` → `3`.

Everything else already matches your fields (`Status`, `Points`, `Dread Bump`,
`Deadline`, `Project`) and options. Research projects are picked up automatically
from the `Project` dropdown, so new ones appear without edits. Optional tweaks:

- `UPCOMING_STATUSES`: remove `"Paused / Background"` to leave paused tasks out of the forecast.
- `HOURS_RANGE`: the points-to-hours conversion. Expected time uses each range's midpoint (1 pt = 0.5 h, 2 pt = 1.5 h, …).
- `PROJECT_ORDER`: fix the order/colours of projects in the stacked chart.

### 3. Create an access token
The built-in Actions token can't read Projects, so create a personal one:

1. GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token (classic).
   (Fine-grained tokens can't read projects owned by a personal account.)
2. Scopes: tick **`read:project`** and **`repo`** (`repo` is needed because `research-admin` is presumably private).
3. Set an expiry (e.g. 1 year) and put a reminder in your to-do system to renew it.
4. Copy the token.

### 4. Add the token as a secret
In the dashboard repo: Settings → Secrets and variables → Actions → New repository secret.
Name: `PROJECT_TOKEN`, value: the token.

### 5. Choose where to view it

**Option A: GitHub Pages (a bookmarkable URL)**
Repo Settings → Pages → Build and deployment → Source: **GitHub Actions**.
The page will be at `https://YOUR-USERNAME.github.io/research-dashboard/`.

**Option B: private download only**
Delete the `deploy:` job and the `upload-pages-artifact` step from the workflow.
Each run then leaves a `dashboard` zip on the run's page (Actions tab → latest run → Artifacts),
containing a self-contained HTML file that opens offline.

### 6. Run it
Actions tab → **Build dashboard** → **Run workflow**. After a minute or two the page
is live (A) or downloadable (B). It then rebuilds daily at about 06:15 UK time; press
**Run workflow** any time for a fresh copy. To change the time, edit the `cron` line (UTC).

## How the numbers are worked out

- **Upcoming** = Status is Todo, In Progress or Paused / Background, and the issue is open and not archived.
- **Points** = base (`Points`) + dread (`Dread Bump`). **Hours** use base points only. Tasks with no Points
  value count as 0 points and are flagged on the Expected time card.
- **Completion date** = when the Status field was last changed (i.e. set to Done), in UK time.
  If that's missing, the issue's closed date is used. Set `COMPLETION_DATE_SOURCE = "closed"` to prefer the
  closed date. Note: if you edit the Status of an already-Done task, its completion date moves.
- **Recent pace** = average points per week over the last 4 full weeks, and how many weeks the upcoming
  points would take at that pace.
- Weeks run Monday to Sunday.

## Preview or test locally (optional)

```
pip install -r requirements.txt
python build_dashboard.py --demo --inline-js --out demo.html      # made-up data
PROJECT_TOKEN=ghp_... python build_dashboard.py --out site/index.html   # your data
```

## Troubleshooting

- **"Fill in OWNER_LOGIN and PROJECT_NUMBER"**: step 2 isn't done yet.
- **401 / "Couldn't find project"**: token expired or missing a scope, wrong `OWNER_TYPE`, or wrong project number.
- **Deploy job fails**: Pages source isn't set to GitHub Actions (step 5A), or the repo is private on a free plan.
- **Scheduled runs stop**: GitHub pauses schedules in repos with no activity for 60 days; re-enable on the Actions tab.
- **"Data notes" at the bottom of the page**: a dropdown option the script didn't recognise (e.g. a new Points value).
