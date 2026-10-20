# Deploying the demo

The whole app (backend + UI) is one Docker service, so one host serves both. Two ways to run it:

| | What you get |
|---|---|
| **Render** | Public URL for the backend and the UI, from this repo using `render.yaml` |
| **Docker image** | Build it from the `Dockerfile`; optionally publish to `ghcr.io/<owner>/<repo>` by running the workflow by hand |

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

Every push to `main` runs the unit tests (`.github/workflows/docker-publish.yml`). Publishing the image is
**manual only**: Actions tab → *test-build-publish* → **Run workflow**. After the first publish, make the package
public if you want others to pull it: GitHub → your profile → **Packages** → the image → **Package settings →
Change visibility → Public**.

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker run -p 8000:8000 -e OPENAI_API_KEY=sk-... ghcr.io/<owner>/<repo>:latest     # any provider, see .env.example
# or with no key at all: open http://localhost:8000 and click "Add your API key"
```
