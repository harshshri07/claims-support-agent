# Deploying the demo

The whole app (backend + UI) is one Docker service. Three ways to run it:

| | What you get |
|---|---|
| **Render** (recommended) | Public URL from this repo using `render.yaml` |
| **Docker image** | `ghcr.io/<owner>/<repo>:latest`, built by GitHub Actions on every push to `main` |
| **Vercel + Render** (optional) | UI on Vercel's CDN, backend on Render |

> Why not Vercel for everything? Vercel runs Python as short-lived serverless functions without shared memory,
> and each conversation's state (verification, memory, open claim) lives in the backend's memory. On Vercel a
> conversation could lose its state between messages. The UI is static, so it can be on Vercel; the backend needs
> a long-running server (Render, Fly, Railway, any Docker host).

---

## 1. Model key for the hosted demo (AWS Bedrock)

Create a dedicated IAM user (e.g. `sop-demo-bedrock`) with **only** this policy, then create an access key for it.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeClaudeOnly",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-*",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-*",
        "arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-haiku-4-5-*",
        "arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-sonnet-4-5-*"
      ]
    },
    {
      "Sid": "ListModelsForTheDropdown",
      "Effect": "Allow",
      "Action": ["bedrock:ListInferenceProfiles"],
      "Resource": "*"
    }
  ]
}
```

A Bedrock budget alert (AWS Budgets) is a good extra safety net. The app also limits each visitor
(`RATE_LIMITS`) and lets visitors use the shared key only for the models in `SERVER_KEY_MODELS`. Anything else
requires their own key, which applies only to their own browser tab.

## 2. Render

1. render.com → **New → Blueprint** → connect this GitHub repo. Render reads `render.yaml`.
2. When prompted, paste `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (stored as Render secrets).
3. Deploy. The URL is `https://<service-name>.onrender.com`. Check `/api/health`.

Notes: the free plan sleeps after inactivity, so the first visit can take 30–60 s to wake. Conversations live in
memory and reset when the service restarts.

## 3. Docker image (GitHub Container Registry)

Every push to `main` runs the unit tests, then builds and publishes the image (`.github/workflows/docker-publish.yml`).
After the first run, make the package public: GitHub → your profile → **Packages** → the image → **Package
settings → Change visibility → Public**.

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker run -p 8000:8000 -e OPENAI_API_KEY=sk-... ghcr.io/<owner>/<repo>:latest     # any provider, see .env.example
# or with no key at all: open http://localhost:8000 and click "Add your API key"
```

## 4. Optional: UI on Vercel

1. Deploy the backend on Render first (step 2).
2. Set `window.API_BASE` in `apps/insurance_claims/frontend/config.js` to the Render URL and commit.
3. vercel.com → **Add New → Project** → this repo → **Root Directory** `apps/insurance_claims/frontend`, framework
   **Other**, no build command. `vercel.json` maps the `/static/...` paths.

The backend allows cross-origin calls, so the Vercel UI can talk to it directly.
