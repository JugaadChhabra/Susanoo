<!--
Setup guide for the GitHub connector. [PRD › Connectors and API budget › Setup guides (draft for demo)]
Numbered lines are shown as steps beside the credential fields; other paragraphs are shown as notes.
HTML comments like this one are never shown in the app.

Click-paths a human must check against GitHub's current UI before shipping (labels change):
  A. Avatar (top right) → Settings → Developer settings (bottom of the left sidebar)
  B. Personal access tokens → Fine-grained tokens → Generate new token
  C. Direct link https://github.com/settings/personal-access-tokens/new opens the same form
  D. "Resource owner" picker, "Expiration" picker
  E. "Repository access" → "Only select repositories" → pick one repository
  F. "Permissions" → "Repository permissions" → "Pull requests" → "Read-only"
  G. "Permissions" → "Repository permissions" → "Contents" → "Read-only"
  H. "Metadata" is added automatically as "Read-only" (mandatory)
  I. "Generate token" button, then the token is shown once and starts with github_pat_
  J. Optional writes: "Pull requests" → "Read and write" is what lets the agent post review comments
-->
# Connect GitHub

1. On GitHub, open your avatar (top right) → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**, then click **Generate new token**. Shortcut: [open the new token form](https://github.com/settings/personal-access-tokens/new).
2. Give it a name like "Susanoo", pick an expiration date, and choose the account or organisation that owns the repository as the **Resource owner**.
3. Under **Repository access**, choose **Only select repositories** and pick the one repository you want reviewed. Susanoo can't see anything else.
4. Under **Permissions** → **Repository permissions**, set **Pull requests** to **Read-only** (so the agent can read pull requests and their diffs) and **Contents** to **Read-only** (so it can read the rest of a changed file for context). GitHub adds **Metadata: Read-only** for you.
5. Click **Generate token**, copy it (it starts with github_pat_ and is only shown once), and paste it here with the repository's name, like acme/payments-api.

Want the agent to post its review as comments? Set **Pull requests** to **Read and write** instead, then switch on "Let the agent post comments" here. Until you do, Susanoo only says what it would have posted.

Your token is stored encrypted, is never shown again, and you can revoke it here or on GitHub at any time.
