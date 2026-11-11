#!/usr/bin/env python3
"""Turn a KoboToolbox submission into a PDF (question labels + answers) and
email it to the address entered in the form.

The submission id comes from the repository_dispatch event (sent by a Kobo
REST Service) or from SUBMISSION_ID (manual run); the form and the answers
are always fetched from the Kobo API.
"""
import base64
import html
import json
import mimetypes
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ASSET_UID_RE = re.compile(r"^[A-Za-z0-9]+$")

KOBO_SERVER = os.environ.get("KOBO_SERVER", "https://kf.kobotoolbox.org").rstrip("/")
KOBO_HEADERS = {"Authorization": f"Token {os.environ.get('KOBO_TOKEN', '')}"}

# Question types that hold no answer worth printing.
SKIP_TYPES = {
    "calculate", "start", "end", "today", "deviceid", "username", "phonenumber",
    "simserial", "subscriberid", "audit", "hidden", "note", "start-geopoint",
    "background-audio", "xml-external", "csv-external",
}

CSS = """
body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; color: #222; }
h1 { font-size: 18pt; margin: 0 0 4px; }
.meta { color: #666; font-size: 9pt; margin-bottom: 18px; }
h2 { font-size: 12.5pt; background: #eef1f5; padding: 6px 8px; margin: 18px 0 6px; }
section section h2 { background: none; border-bottom: 1px solid #ccd; padding: 4px 0; }
.q { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #eee;
     page-break-inside: avoid; }
.label { flex: 0 0 42%; font-weight: bold; }
.answer { flex: 1; white-space: pre-wrap; word-break: break-word; }
.answer img { max-width: 100%; max-height: 90mm; }
.repeat-item { border-left: 3px solid #ccd; padding-left: 10px; margin: 8px 0; }
.repeat-no { color: #666; font-size: 9pt; }
ul { margin: 0; padding-left: 18px; }
"""


def env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value or default


def kobo_request(path_or_url, **params):
    url = path_or_url if path_or_url.startswith("http") else f"{KOBO_SERVER}{path_or_url}"
    r = requests.get(url, headers=KOBO_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r


def kobo_get(path, **params):
    return kobo_request(path, **params).json()


def trigger():
    """Return (asset_uid, submission_id) for this run.

    Only these two ids are taken from the trigger. Everything else (including
    the recipient) is re-read from Kobo, so a forged trigger can't redirect a
    submission to a different address.
    """
    event_path = env("GITHUB_EVENT_PATH")
    if env("GITHUB_EVENT_NAME") == "repository_dispatch" and event_path:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        submission = event["client_payload"]["submission"]
        # Kobo puts the project (asset) uid in every submission it sends.
        asset_uid = submission.get("_xform_id_string") or env("KOBO_ASSET_UID")
        raw_id = submission["_id"]
    else:
        asset_uid = env("KOBO_ASSET_UID")
        raw_id = env("SUBMISSION_ID")

    if not asset_uid or not ASSET_UID_RE.match(asset_uid):
        sys.exit(f"Missing or invalid Kobo project uid: {asset_uid!r}")
    allowed = [u.strip() for u in (env("ALLOWED_ASSET_UIDS") or "").split(",") if u.strip()]
    if allowed and asset_uid not in allowed:
        sys.exit(f"Project {asset_uid} is not in ALLOWED_ASSET_UIDS, nothing sent.")
    return asset_uid, int(raw_id)


def find_recipient(submission):
    # EMAIL_FIELD may list several question names (one per form), comma-separated.
    fields = [f.strip() for f in (env("EMAIL_FIELD") or "").split(",") if f.strip()]
    if fields:
        # Kobo prefixes grouped questions with their group path, e.g. "contact/email".
        for field in fields:
            for key, value in submission.items():
                if key == field or key.endswith("/" + field):
                    candidate = str(value).strip()
                    if EMAIL_RE.match(candidate):
                        return candidate
        return None

    # No field configured: take the first answer that looks like an email address.
    for key, value in submission.items():
        if not key.startswith("_") and isinstance(value, str) and EMAIL_RE.match(value.strip()):
            return value.strip()
    return None


# ---------------------------------------------------------------- PDF content

def item_name(item):
    # Choices built in the Kobo form builder may only have "$autovalue".
    return item.get("name") or item.get("$autoname") or item.get("$autovalue") or ""


def plain_text(label):
    """Strip the Markdown/HTML styling Kobo allows in labels, e.g.
    '### <span style="color:red">Title</span>' -> 'Title'."""
    text = re.sub(r"<[^>]*>", "", label)                          # HTML tags
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)     # headings
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # links
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)               # bold
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)  # *italic*
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)          # _italic_
    return html.unescape(text).strip()


class Form:
    """The parts of the Kobo form definition needed to label answers."""

    def __init__(self, asset):
        content = asset.get("content", {})
        self.title = asset.get("name") or "Form submission"
        self.lang = self._language_index(content.get("translations") or [None])
        self.tree = self._build_tree(content.get("survey", []))
        self.choices = {}
        self.choice_lists = {}  # list name -> choices in form order (matrix rows)
        for c in content.get("choices", []):
            self.choices[(c.get("list_name"), item_name(c))] = self.label(c)
            self.choice_lists.setdefault(c.get("list_name"), []).append(c)

    @staticmethod
    def _language_index(translations):
        wanted = (env("FORM_LANGUAGE") or "").lower()
        for i, name in enumerate(translations):
            if wanted and name and wanted in name.lower():
                return i
        return 0

    @staticmethod
    def _build_tree(rows):
        # Kobo stores the survey flat with begin/end markers; nest it.
        root = {"children": []}
        stack = [root]
        for row in rows:
            kind = row.get("type", "").replace(" ", "_")
            if kind in ("begin_group", "begin_repeat", "begin_kobomatrix"):
                node = {
                    "row": row,
                    "repeat": kind == "begin_repeat",
                    "matrix": kind == "begin_kobomatrix",
                    "children": [],
                }
                stack[-1]["children"].append(node)
                stack.append(node)
            elif kind in ("end_group", "end_repeat", "end_kobomatrix"):
                if len(stack) > 1:
                    stack.pop()
            else:
                stack[-1]["children"].append({"row": row})
        return root["children"]

    def text(self, item):
        """The item's label as plain text, or "" if it has none."""
        label = item.get("label")
        if isinstance(label, list):
            picked = label[self.lang] if self.lang < len(label) else None
            label = picked or next((l for l in label if l), None)
        return plain_text(label or "")

    def label(self, item):
        """The label, falling back to the question name so an answer is never unlabelled."""
        return self.text(item) or item_name(item)


class Renderer:
    def __init__(self, form, submission):
        self.form = form
        self.submission = submission
        self.attachments = submission.get("_attachments", [])

    def render(self):
        submitted = self.submission.get("_submission_time", "")
        try:
            submitted = datetime.fromisoformat(submitted).strftime("%d %B %Y, %H:%M UTC")
        except ValueError:
            pass
        body = self.nodes(self.form.tree, self.submission, "")
        return (
            f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<h1>{html.escape(self.form.title)}</h1>"
            f"<div class='meta'>Submitted: {html.escape(submitted)} &nbsp;·&nbsp; "
            f"Submission #{self.submission['_id']}</div>{body}</body></html>"
        )

    def nodes(self, nodes, data, prefix, matrix_row=None):
        out = []
        for node in nodes:
            row = node["row"]
            path = prefix + item_name(row)
            if node.get("matrix"):
                out.append(self.matrix(node, data, prefix))
                continue
            if "children" in node:
                heading, row_name = self.group_heading(node)
                if node["repeat"]:
                    items = data.get(path) or []
                    inner = "".join(
                        f"<div class='repeat-item'><div class='repeat-no'>#{i}</div>"
                        f"{self.nodes(node['children'], item, path + '/')}</div>"
                        for i, item in enumerate(items, 1)
                    )
                else:
                    inner = self.nodes(node["children"], data, path + "/", row_name)
                if inner:  # skip groups with no answered questions
                    out.append(self.section(heading, inner))
                continue

            kind = row.get("type", "")
            value = data.get(path)
            if kind in SKIP_TYPES or value in (None, ""):
                continue
            label = self.form.label(row)
            if matrix_row:
                # Matrix cells are labelled "<row>-<column>"; the row is already the heading.
                label = label.removeprefix(matrix_row + "-")
            out.append(self.question(label, row, kind, value))
        return "".join(out)

    def group_heading(self, node):
        """Return (heading, matrix row name) for a group.

        A Kobo Question Matrix becomes one unlabelled group per row (named
        "<matrix>_<row>") whose row label sits in a "<group>_note" note. Use that
        note as the heading instead of printing the internal group name.
        """
        row = node["row"]
        heading = self.form.text(row)
        if heading:
            return heading, None
        name = item_name(row)
        for child in node["children"]:
            child_row = child["row"]
            if "children" not in child and item_name(child_row) == name + "_note":
                note = self.form.text(child_row)
                # The cells' labels start with the row's choice name: "<row>-<column>".
                for cell in node["children"]:
                    cell_label = self.form.text(cell["row"])
                    row_name, sep, _ = cell_label.partition("-")
                    if sep and name.endswith("_" + row_name):
                        return note, row_name
                return note, None
        return "", None  # no label at all: show the answers without a heading

    def matrix(self, node, data, prefix):
        """A Question Matrix as stored in the Kobo form: one column question per
        child, one row per choice of its "kobo--matrix_list". Kobo saves each
        cell as "<matrix>_<row>/<matrix>_<row>_<column>"."""
        row = node["row"]
        matrix_name = item_name(row)
        rows = []
        for choice in self.form.choice_lists.get(row.get("kobo--matrix_list"), []):
            base = f"{matrix_name}_{item_name(choice)}"
            cells = []
            for col in node["children"]:
                col_row = col["row"]
                kind = col_row.get("type", "")
                value = data.get(f"{prefix}{base}/{base}_{item_name(col_row)}")
                if "children" in col or kind in SKIP_TYPES or value in (None, ""):
                    continue
                cells.append(self.question(self.form.label(col_row), col_row, kind, value))
            if cells:
                rows.append(self.section(self.form.label(choice), "".join(cells)))
        return self.section(self.form.text(row), "".join(rows)) if rows else ""

    @staticmethod
    def section(heading, inner):
        title = f"<h2>{html.escape(heading)}</h2>" if heading else ""
        return f"<section>{title}{inner}</section>"

    def question(self, label, row, kind, value):
        return (
            f"<div class='q'><div class='label'>{html.escape(label)}</div>"
            f"<div class='answer'>{self.answer(row, kind, value)}</div></div>"
        )

    def answer(self, row, kind, value):
        list_name = row.get("select_from_list_name")
        if kind == "select_one":
            return html.escape(self.form.choices.get((list_name, str(value)), str(value)))
        if kind == "select_multiple":
            labels = [self.form.choices.get((list_name, v), v) for v in str(value).split()]
            return "<ul>" + "".join(f"<li>{html.escape(l)}</li>" for l in labels) + "</ul>"
        if kind == "image":
            img = self.image(str(value))
            if img:
                return img
        return html.escape(str(value))

    def image(self, filename):
        for att in self.attachments:
            stored = att.get("filename", "")
            if stored == filename or stored.endswith("/" + filename) or att.get("media_file_basename") == filename:
                url = att.get("download_large_url") or att.get("download_url")
                if not url:
                    return None
                data = kobo_request(url).content
                mime = att.get("mimetype") or "image/jpeg"
                return f"<img src='data:{mime};base64,{base64.b64encode(data).decode()}'>"
        return None


def render_pdf(page_html, out_path):
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")  # preinstalled on GitHub runners
        page = browser.new_page()
        page.set_content(page_html, wait_until="load")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "14mm", "right": "14mm"},
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=(
                "<div style='font-size:8pt;color:#888;width:100%;text-align:center'>"
                "Page <span class='pageNumber'></span> of <span class='totalPages'></span></div>"
            ),
        )
        browser.close()


# ---------------------------------------------------------------- email

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_SRC_RE = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.I | re.S)


def html_to_text(markup):
    """Rough plain-text version of the signature for mail apps that show no HTML."""
    text = re.sub(r"(?is)<(style|script)\b.*?</\1>", "", markup)
    text = re.sub(r"(?is)<img\b[^>]*>", "", text)
    text = re.sub(r"\s+", " ", text)  # line breaks in the HTML source mean nothing
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li|table)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in html.unescape(text).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def add_signed_body(msg, body, signature_html):
    """Plain-text + HTML body with the signature. Images in the signature that
    point to a file in this repository (e.g. src="assets/logo.png") are attached
    inline, so they show without the reader having to allow remote images."""
    images = []  # (cid, path)

    def embed(match):
        src = match.group(3).strip()
        if re.match(r"^(https?:|cid:|data:)", src, re.I):
            return match.group(0)
        path = (REPO_ROOT / src).resolve()
        if REPO_ROOT not in path.parents or not path.is_file():
            sys.exit(f"MAIL_SIGNATURE_HTML: image {src!r} not found in the repository")
        cid = make_msgid(domain="signature")
        images.append((cid, path))
        return f"{match.group(1)}{match.group(2)}cid:{cid[1:-1]}{match.group(2)}"

    signature_html = IMG_SRC_RE.sub(embed, signature_html)
    body_html = html.escape(body).replace("\n", "<br>\n")

    msg.set_content(f"{body.rstrip()}\n\n-- \n{html_to_text(signature_html)}\n")
    msg.add_alternative(
        "<!doctype html><html><body>"
        f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:11pt'>{body_html}</div>"
        f"<br>{signature_html}</body></html>",
        subtype="html",
    )
    html_part = msg.get_payload()[1]
    for cid, path in images:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        maintype, subtype = mime.split("/", 1)
        html_part.add_related(path.read_bytes(), maintype=maintype, subtype=subtype,
                              cid=cid, filename=path.name, disposition="inline")


def send_mail(recipient, pdf_path, submission, form_title):
    def fill(text):
        return text.replace("{form}", form_title)

    msg = EmailMessage()
    sender = env("MAIL_FROM", env("SMTP_USERNAME"))
    if env("MAIL_FROM_NAME"):
        sender = formataddr((env("MAIL_FROM_NAME"), parseaddr(sender)[1]))
    msg["From"] = sender
    msg["To"] = recipient
    if env("MAIL_BCC"):
        msg["Bcc"] = env("MAIL_BCC")
    if env("MAIL_REPLY_TO"):
        # Send from one account (e.g. a sending-only Gmail), receive replies at another.
        msg["Reply-To"] = env("MAIL_REPLY_TO")
    msg["Subject"] = fill(env("MAIL_SUBJECT", "Your submission: {form}"))
    body = fill(env(
        "MAIL_BODY",
        "Hello,\n\nThank you for your submission to \"{form}\". "
        "Please see a copy of your responses attached. For any concerns, please contact MEL@australiaassists.org.\n",
    ))
    signature = env("MAIL_SIGNATURE_HTML")
    if signature:
        add_signed_body(msg, body, fill(signature))
    else:
        msg.set_content(body)
    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=f"submission-{submission['_id']}.pdf",
    )

    oauth = bool(env("OUTLOOK_CLIENT_ID") and env("OUTLOOK_REFRESH_TOKEN"))
    host = env("SMTP_HOST", "smtp-mail.outlook.com" if oauth else None)
    port = int(env("SMTP_PORT", "587" if oauth else "465"))
    user, password = env("SMTP_USERNAME"), env("SMTP_PASSWORD")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls()
    with server:
        if oauth:
            # Outlook.com no longer accepts passwords over SMTP, only OAuth2.
            xoauth2_login(server, user, outlook_access_token())
        elif user:
            server.login(user, password)
        server.send_message(msg)


# ---------------------------------------------------------------- Outlook OAuth2

OUTLOOK_SCOPE = "https://outlook.office.com/SMTP.Send offline_access"


def outlook_token_url():
    # "consumers" = personal Outlook.com/Hotmail accounts; a work tenant id also works.
    return f"https://login.microsoftonline.com/{env('OUTLOOK_TENANT', 'consumers')}/oauth2/v2.0/token"


def outlook_access_token():
    """Swap the stored refresh token for a short-lived access token."""
    r = requests.post(outlook_token_url(), timeout=60, data={
        "client_id": env("OUTLOOK_CLIENT_ID"),
        "grant_type": "refresh_token",
        "refresh_token": env("OUTLOOK_REFRESH_TOKEN"),
        "scope": OUTLOOK_SCOPE,
    })
    if r.status_code != 200:
        error = r.json().get("error_description", r.text) if r.headers.get(
            "content-type", "").startswith("application/json") else r.text
        sys.exit(
            "Outlook sign-in failed; run scripts/outlook_oauth_setup.py again to get a "
            f"new OUTLOOK_REFRESH_TOKEN.\n{error.splitlines()[0] if error else r.status_code}"
        )
    tokens = r.json()
    new_refresh = tokens.get("refresh_token")
    if new_refresh and new_refresh != env("OUTLOOK_REFRESH_TOKEN"):
        print(f"::add-mask::{new_refresh}")  # never show it in the run log
        save_refresh_token(new_refresh)
    return tokens["access_token"]


def save_refresh_token(token):
    """Microsoft hands out a new refresh token on every use, and each one only
    lasts 90 days. With GH_SECRETS_TOKEN set, store the new one in the
    OUTLOOK_REFRESH_TOKEN secret so the sign-in never runs out."""
    gh_token, repo = env("GH_SECRETS_TOKEN"), env("GITHUB_REPOSITORY")
    if not gh_token or not repo:
        return
    try:
        from nacl import encoding, public

        api = f"https://api.github.com/repos/{repo}/actions/secrets"
        headers = {"Authorization": f"Bearer {gh_token}",
                   "Accept": "application/vnd.github+json"}
        r = requests.get(f"{api}/public-key", headers=headers, timeout=30)
        r.raise_for_status()
        key = r.json()
        box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
        sealed = base64.b64encode(box.encrypt(token.encode())).decode()
        r = requests.put(f"{api}/OUTLOOK_REFRESH_TOKEN", headers=headers, timeout=30,
                         json={"encrypted_value": sealed, "key_id": key["key_id"]})
        r.raise_for_status()
        print("Saved the renewed Outlook refresh token.")
    except Exception as exc:  # sending the email matters more than this
        print(f"::warning::Could not save the renewed Outlook refresh token: {exc}")


def xoauth2_login(server, user, access_token):
    auth = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    server.ehlo()
    # smtplib base64-encodes the answer; on an error challenge, reply empty.
    server.auth("XOAUTH2", lambda challenge=None: auth if challenge is None else "")


def mask(address):
    name, _, domain = address.partition("@")
    return f"{name[:2]}***@{domain}"


def main():
    asset_uid, sub_id = trigger()
    submission = kobo_get(f"/api/v2/assets/{asset_uid}/data/{sub_id}/", format="json")

    recipient = find_recipient(submission)
    if not recipient:
        print(f"Submission {sub_id}: no valid email address found, nothing sent.")
        return

    form = Form(kobo_get(f"/api/v2/assets/{asset_uid}/", format="json"))
    pdf_path = Path(env("RUNNER_TEMP", ".")) / f"submission-{sub_id}.pdf"
    render_pdf(Renderer(form, submission).render(), pdf_path)
    send_mail(recipient, pdf_path, submission, form.title)
    # Logs may be visible to repo collaborators, so don't print the full address.
    print(f"Project {asset_uid}, submission {sub_id}: PDF sent to {mask(recipient)}")


if __name__ == "__main__":
    main()
