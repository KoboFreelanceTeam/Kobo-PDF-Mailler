# Kobo submission → PDF → email

When a form is submitted in KoboToolbox, a Kobo **REST Service** calls the GitHub
API, which starts the workflow in `.github/workflows/kobo-pdf-email.yml`. The workflow:

1. downloads the submission and the form definition from the Kobo API,
2. lays out every answered question (label + answer, grouped by section,
   choice labels instead of codes, photos embedded) and prints it to PDF
   with headless Chromium (Playwright),
3. emails the PDF to the address entered in the form.

The Enketo "view" page is not used: it only loads for someone logged in to
Kobo, which the GitHub runner is not.

```
Kobo submission ──REST Service──▶ api.github.com/repos/<owner>/<repo>/dispatches
                                        │
                                        ▼
                           GitHub Action (repository_dispatch)
              Kobo API (form + answers) → HTML → Chromium → PDF → SMTP
```

> Use a **private** repository: submission data passes through the Actions run.

## 1. Repository settings

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Value |
|---|---|
| `KOBO_TOKEN` | Kobo API token (Kobo → Account settings → Security → API key) |
| `SMTP_USERNAME` | SMTP login, e.g. your Gmail address |
| `SMTP_PASSWORD` | SMTP password; for Gmail an [App Password](https://myaccount.google.com/apppasswords). Not used with Outlook (see [Sending from Outlook.com](#sending-from-outlookcom)) |
| `OUTLOOK_REFRESH_TOKEN` | Outlook only: from `scripts/outlook_oauth_setup.py` |
| `GH_SECRETS_TOKEN` | Outlook only, optional: lets the workflow renew `OUTLOOK_REFRESH_TOKEN` itself |

**Settings → Secrets and variables → Actions → Variables**

| Variable | Example | Notes |
|---|---|---|
| `KOBO_SERVER` | `https://kf.kobotoolbox.org` | `https://eu.kobotoolbox.org` for the EU server |
| `KOBO_ASSET_UID` | `aBcD123...` | Only for **manual** runs: the project used when none is entered. Kobo triggers carry their own project uid (`_xform_id_string`). From the project URL: `.../#/forms/<ASSET_UID>/...` |
| `ALLOWED_ASSET_UIDS` | `aBcD123,eFgH456` | Optional, comma-separated. If set, submissions from any other project are ignored. |
| `EMAIL_FIELD` | `email, contact_email` | Name(s) of the email question, comma-separated if your forms use different names (group prefix not needed). If empty, the first answer that looks like an email is used. |
| `FORM_LANGUAGE` | `English` | Optional, for multi-language forms: which label language to print |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `465` | `465` = SSL, anything else = STARTTLS (e.g. `587`) |
| `OUTLOOK_CLIENT_ID` | `1a2b3c4d-...` | Outlook only: the Azure app's Application (client) ID |
| `OUTLOOK_TENANT` | `consumers` | Outlook only, optional: `consumers` (default) for personal accounts, or a work tenant id |
| `MAIL_FROM` | `Survey Team <you@gmail.com>` | Optional, defaults to `SMTP_USERNAME` |
| `MAIL_FROM_NAME` | `Survey Team` | Optional sender name shown in the inbox; the address stays `MAIL_FROM` / `SMTP_USERNAME` |
| `MAIL_SUBJECT` | `Your submission: {form}` | Optional; `{form}` is replaced by the form's name |
| `MAIL_BODY` | | Optional plain-text body; `{form}` works here too |
| `MAIL_SIGNATURE_HTML` | contents of `assets/signature.html` | Optional HTML signature added below the body (see [Email signature](#email-signature)) |
| `MAIL_BCC` | `archive@example.org` | Optional, get a copy of every email |
| `MAIL_REPLY_TO` | `replies@example.org` | Optional: where replies go when someone clicks Reply. Lets you send from a sending-only account (see [Sending-only Gmail account](#sending-only-gmail-account)) |

### Email signature

Gmail adds your signature only to mail written in Gmail, not to mail sent over
SMTP, so set it here instead:

1. Edit [`assets/signature.html`](assets/signature.html) (or write your own HTML).
2. Copy the whole file into the `MAIL_SIGNATURE_HTML` variable.

The email is then sent as HTML (with a plain-text copy for mail apps that need
one). Images whose `src` is a file in this repository, e.g.
`<img src="assets/australian-aid.png">`, are embedded in the email, so they show
without the reader having to allow remote images. `https://` image links are
left as they are. To change a logo, replace the file in `assets/` and push.

### Sending-only Gmail account

Gmail App Passwords need 2-Step Verification on the account. If the person whose
address should receive replies doesn't want that on their own account, send from
a separate Gmail account that is only used by this workflow:

1. Create a new Gmail account, e.g. `survey-mailer@gmail.com`.
2. Turn on 2-Step Verification for **that** account
   ([myaccount.google.com/security](https://myaccount.google.com/security)).
3. Create an App Password for it
   ([myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)).
4. Set the secrets `SMTP_USERNAME` = the new address, `SMTP_PASSWORD` = the App
   Password (16 letters, spaces don't matter), and the variables
   `SMTP_HOST` = `smtp.gmail.com`, `SMTP_PORT` = `465`.
5. Set `MAIL_FROM_NAME` (e.g. `Survey Team`) so the inbox shows a
   name instead of the address, and `MAIL_REPLY_TO` to the address that should
   get replies (e.g. `replies@example.org`).

Recipients see the team name as sender; when they reply, the reply goes to
`MAIL_REPLY_TO`, not to the sending account.

### Sending from Outlook.com

Personal Outlook.com / Hotmail accounts no longer accept a password over SMTP
(error `5.7.139 ... SmtpClientAuthentication is disabled`). They need OAuth2,
which the script uses when `OUTLOOK_CLIENT_ID` and `OUTLOOK_REFRESH_TOKEN` are set.

1. **Register an app** at [portal.azure.com](https://portal.azure.com) →
   *App registrations* → *New registration*:
   - Supported account types: *Personal Microsoft accounts only*
   - No redirect URI.
   - After creating it: *Authentication* → *Allow public client flows* → **Yes** → Save.
   - Copy the *Application (client) ID* into the `OUTLOOK_CLIENT_ID` variable.
2. **Sign in once** on your computer (Python with `requests` installed, and the
   [GitHub CLI](https://cli.github.com) logged in to this repo):

   ```
   python scripts/outlook_oauth_setup.py <CLIENT_ID> | gh secret set OUTLOOK_REFRESH_TOKEN
   ```

   Open the link it shows, enter the code and sign in with the Outlook account
   that should send the emails.
3. Set `SMTP_USERNAME` to that Outlook address. `SMTP_HOST` / `SMTP_PORT` default
   to `smtp-mail.outlook.com` / `587`; `SMTP_PASSWORD` is not needed.
4. **Keep it alive (recommended).** Each refresh token lasts 90 days, and
   Microsoft gives a new one on every use. Create a fine-grained GitHub token
   for this repo with **Secrets → Read and write** and save it as the
   `GH_SECRETS_TOKEN` secret; the workflow then stores each new refresh token
   itself. Without it, repeat step 2 within 90 days.

If the run log says *Outlook sign-in failed*, repeat step 2.

## 2. GitHub token for Kobo

Create a **fine-grained personal access token**
(GitHub → Settings → Developer settings → Fine-grained tokens):

- Repository access: only this repository
- Permissions: **Contents → Read and write** (required for `repository_dispatch`)

## 3. Kobo REST Service

Do this **once per Kobo project** that should email PDFs; the settings are the
same for every project, and the one workflow handles them all.

In the Kobo project: **Settings → REST Services → Register a new service**

- **Endpoint URL:** `https://api.github.com/repos/<owner>/<repo>/dispatches`
- **Type:** JSON
- **Security:** Basic Authorization, username = your GitHub username,
  password = your fine-grained token (a custom `Authorization` header did not
  reach GitHub from Kobo)
- **Custom HTTP Headers:**
  - `Accept` → `application/vnd.github+json`
- **Add custom wrapper around JSON submission:**

  ```
  {"event_type": "kobo_submission", "client_payload": {"submission": %SUBMISSION%}}
  ```

  The wrapper is required: GitHub expects this shape and allows at most 10
  top-level keys in `client_payload`, so the submission is nested under one key.

GitHub answers `204 No Content` on success; Kobo shows the status per submission
in the REST Service log, where failed calls can be retried.

## Privacy: who can see what

| Place | Who can see it | Protection |
|---|---|---|
| Emailed PDF | Only the address typed in the form (+ `MAIL_BCC`) | Recipient is always re-read from Kobo, never taken from the trigger |
| Actions run logs | People with access to the GitHub repo | Logs contain only the submission id and a masked email (`jo***@example.org`); no answers, no view link |
| The PDF file | Nobody | Created on a throwaway GitHub runner, deleted when the job ends; not uploaded as an artifact |
| `KOBO_TOKEN` | Nobody (masked secret) | But anyone with **write** access to the repo could change the workflow and use it, so give write access only to people allowed to see the data |
| GitHub token in Kobo | Kobo project admins | They can already see all submissions. Someone holding it can only trigger an email of a submission to *its own* recipient |

Recommended:
- Keep the repo **private** with as few collaborators as possible.
- Settings → Actions → General → set **log retention** to a few days.
- If you use `MAIL_BCC`, that mailbox receives every submission.

## 4. Test

- Manually: **Actions → Kobo submission PDF email → Run workflow**, enter a
  submission `_id` (the number shown in Kobo's data table / API).
- End to end: submit the form with your own email address.
