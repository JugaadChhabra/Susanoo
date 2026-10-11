<!--
Setup guide for the Anthropic connector. [PRD › Connectors and API budget › Setup guides (draft for demo)]
Numbered lines are shown as steps beside the credential fields; other paragraphs are shown as notes.
HTML comments like this one are never shown in the app.

Click-paths a human must check against the current Claude Console before shipping (labels change;
the Anthropic Console moved from console.anthropic.com to platform.claude.com and is now called the Claude Console):
  A. https://platform.claude.com signs in to the Console (console.anthropic.com redirects there)
  B. Settings (left sidebar or account menu) → "API keys"
  C. Direct link https://platform.claude.com/settings/keys opens the same page
  D. "Create Key" button, then the workspace picker and the key name field, then "Create Key" again (or "Add")
  E. The key is shown once, with a copy button, and starts with sk-ant-api03-
  F. Settings → "Workspaces" → "Create Workspace" (optional; lets a spend limit cover only this key)
  G. Settings → "Workspaces" → pick the workspace → "Limits" tab → "Change Limit" sets its monthly spend limit
  H. Settings → "Limits" → organisation-wide monthly spend limit (the fallback if you skip the workspace)
  I. "Admin keys" live on a separate page; the app rejects keys starting sk-ant-admin
-->
# Connect Anthropic

1. In the Claude Console (formerly the Anthropic Console), open **Settings** → **API keys**. Shortcut: [open your API keys](https://platform.claude.com/settings/keys).
2. Click **Create Key**, name it "Susanoo" and pick a workspace. Tip: make a workspace just for Susanoo first (**Settings** → **Workspaces** → **Create Workspace**), so the spend limit in step 4 covers only this key.
3. Click **Create Key**, then copy the key. It starts with sk-ant- and is only shown once.
4. Set a monthly spend limit: **Settings** → **Workspaces** → your Susanoo workspace → **Limits** → **Change Limit**. This is your agent's cap: Anthropic stops calls past it, whatever Susanoo does. No separate workspace? Set it for your whole organisation under **Settings** → **Limits** instead.
5. Paste the key here. Susanoo checks it straight away by listing models, which costs nothing.

Don't want to use your own key? Skip this, and your agent runs on Susanoo's key with a small cap per run and a few runs a day.

Your key is stored encrypted, is never shown again, never goes into your agent's code or our logs, and you can revoke it here or in the Claude Console at any time.
