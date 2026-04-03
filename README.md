# The Sommelier — Deployment Guide

A private wine and food pairing assistant powered by Claude and François Chartier's molecular pairing methodology.

> **Keep your repository private.** The `somm_data/` folder contains content extracted from a copyrighted book. Do not make the repository public.

---

## Section 1: One-Time Setup

### 1.1 Get a Claude API key
1. Go to [console.anthropic.com](https://console.anthropic.com) and create an account
2. Click **API Keys** in the left sidebar, then **Create Key**
3. Copy the key (it starts with `sk-ant-`) — you won't see it again
4. Go to **Billing** and add $10 of credit (this covers roughly 50–200 questions)

### 1.2 Create a GitHub account
If you don't have one, go to [github.com](https://github.com) and sign up (free).

### 1.3 Create a Render.com account
Go to [render.com](https://render.com) and sign up with your GitHub account (free, no credit card needed).

---

## Section 2: Prepare the Repository

### 2.1 Create a new GitHub repository
1. On GitHub, click the **+** icon → **New repository**
2. Name it `somm`
3. Set it to **Private**
4. Do **not** initialize with a README
5. Click **Create repository**

### 2.2 Set up the files on your computer

Open Terminal and run these commands one at a time:

```bash
# Go to your home folder (or wherever you want to work)
cd ~

# Clone your new empty repository
git clone https://github.com/YOUR-USERNAME/somm.git
cd somm
```

Replace `YOUR-USERNAME` with your actual GitHub username.

### 2.3 Copy the app files
Copy everything from the `somm-app/` folder into the `somm/` repository folder you just cloned.

Your repository folder should now contain:
```
somm/
├── main.py
├── build_context.py
├── requirements.txt
├── .env.example
├── .gitignore
├── render.yaml
├── static/
│   └── index.html
└── README.md        ← this file
```

### 2.4 Add the reference data
1. Create a folder called `somm_data` inside the repository
2. Copy the **entire contents** of the `Wine Pairings/somm/` folder into `somm_data/`

The result should look like:
```
somm/
└── somm_data/
    ├── SKILL.md
    ├── references/
    │   ├── Table_1___molecules_to_aromas_t.md
    │   ├── Table_2___molecules_to_aromas_t.md
    │   ├── Table_3___Master_Pairing.md
    │   ├── Table_4___Cooking_Transformatio.md
    │   ├── Table_5___wines_to_aromas.md
    │   ├── Table_6___Physiological_Effects.md
    │   └── book_citations.md
    └── screenshots/
        ├── INDEX.md
        └── (37 PNG files)
```

### 2.5 Commit and push

```bash
git add .
git commit -m "Initial somm app"
git push origin main
```

---

## Section 3: Deploy to Render.com

1. Go to [render.com](https://render.com) and log in
2. Click **New** → **Web Service**
3. Click **Connect account** next to GitHub and authorize Render
4. Find and select your `somm` repository, click **Connect**
5. Render will auto-detect the `render.yaml` file — you don't need to change build settings
6. Scroll down to **Environment Variables** and add these two:
   - `ANTHROPIC_API_KEY` → paste your API key (the one starting with `sk-ant-`)
   - `APP_PASSWORD` → choose any password to share with friends (e.g. `burgundy2024`)
   - `SOMM_DATA_PATH` should already be set to `./somm_data` from the config file
7. Click **Create Web Service**
8. Wait about 2 minutes for the build to complete
9. When the status shows **Live**, copy the URL (e.g. `https://somm.onrender.com`)

---

## Section 4: Share Access

- Share the URL and the `APP_PASSWORD` you chose with your friends
- They open the URL in any browser, enter the password, and start asking questions

**Note:** The app is on Render's free tier, which means it sleeps after 15 minutes of inactivity. The first request after sleeping takes about 30 seconds to wake up — this is normal.

---

## Section 5: Costs

| Item | Cost |
|------|------|
| Hosting (Render free tier) | $0/month |
| Claude API — simple text query | ~$0.05 per question |
| Claude API — query with screenshots | ~$0.10–0.20 per question |
| $10 API credit covers | ~50–200 questions |

You can monitor your API usage at [console.anthropic.com](https://console.anthropic.com).

---

## Section 6: Updating the Skill

If the reference files (SKILL.md, tables, or screenshots) are updated:

1. Copy the updated files into `somm_data/` in your repository folder
2. Open Terminal, go to your `somm` folder, and run:

```bash
git add somm_data/
git commit -m "Update skill data"
git push origin main
```

3. Render will automatically detect the push and redeploy within 2 minutes

---

## Running Locally (for testing)

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your real API key and password

# Start the server
uvicorn main:app --reload

# Open http://localhost:8000 in your browser
```

To verify the system prompt assembles correctly:
```bash
python build_context.py --path "../Wine Pairings/somm"
```
