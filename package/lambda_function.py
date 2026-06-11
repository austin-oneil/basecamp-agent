import json
import re
import boto3
from datetime import datetime
import basecamp as bc
from classifier import classify

secretsmanager = boto3.client("secretsmanager", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
s3 = boto3.client("s3", region_name="us-east-1")
table = dynamodb.Table("basecamp-seen-todos")

S3_BUCKET = "basecamp-agent-standards-423129721363"
EMAIL_SIZE_LIMIT = 8000
SKIP_CATEGORIES = {"misc"}

# ── Credentials ────────────────────────────────────────────────────────────────

def get_credentials():
    secret = secretsmanager.get_secret_value(SecretId="basecamp-agent/credentials")
    return json.loads(secret["SecretString"])

# ── S3 Helpers ─────────────────────────────────────────────────────────────────

def load_s3_file(key):
    """Load a file from S3. Returns content as string or None if not found."""
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return response["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        print(f"  S3 load failed for {key}: {e}")
        return None

def extract_client_code(project_name):
    """Extract client code from project name e.g. 'JG - Jeff Gray' -> 'JG'"""
    match = re.match(r"^([A-Z]+)\s*-", project_name)
    return match.group(1) if match else None

def upload_draft_to_s3(todo, output, creds):
    """Upload full draft to S3 and return a presigned URL valid for 7 days."""
    s3_client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id=creds.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=creds.get("AWS_SECRET_ACCESS_KEY"),
    )
    key = f"drafts/{todo['id']}.txt"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=output.encode("utf-8"),
        ContentType="text/plain",
    )
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=604800,  # 7 days
    )
    return url

# ── Page Crawler ───────────────────────────────────────────────────────────────

def find_page_url(todo, client_content):
    """
    Try to find a relevant page URL from the client file based on the task title.
    Looks for URLs in the Core Services section that match keywords in the task title.
    """
    title_lower = todo["title"].lower()

    service_hints = {
        "sedation": "sedation",
        "implant": "implant",
        "veneer": "veneer",
        "invisalign": "invisalign",
        "whitening": "whitening",
        "crown": "crown",
        "emergency": "emergency",
        "root canal": "root-canal",
        "extraction": "extraction",
        "wisdom": "wisdom",
        "cleaning": "cleaning",
        "gum": "gum",
        "cosmetic": "cosmetic",
        "denture": "denture",
        "oral surgery": "oral-surgery",
        "filling": "filling",
        "bonding": "bonding",
    }

    matched_hint = None
    for keyword, hint in service_hints.items():
        if keyword in title_lower:
            matched_hint = hint
            break

    if not matched_hint:
        return None

    urls = re.findall(r'https?://[^\s\)]+', client_content)
    for url in urls:
        if matched_hint in url.lower():
            return url

    return None


def fetch_page_content(url):
    """Fetch and return plain text content from a URL for SEO auditing."""
    import urllib.request
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text = []
            self.in_skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.in_skip = False

        def handle_data(self, data):
            if not self.in_skip:
                stripped = data.strip()
                if stripped:
                    self.text.append(stripped)

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "BasecampAgent SEO Auditor"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8", errors="ignore")
            parser = TextExtractor()
            parser.feed(html)
            content = " ".join(parser.text)
            return content[:5000]
    except Exception as e:
        print(f"  Page fetch failed for {url}: {e}")
        return None

# ── Context Loading ────────────────────────────────────────────────────────────

def load_context(category, client_code, todo=None):
    """Load standards doc, client file, keyword file, and current page content."""
    ctx = {}

    standards = load_s3_file(f"standards/{category}.md")
    if standards:
        ctx["standards"] = standards
    else:
        print(f"  No standards doc found for category: {category}")

    if client_code:
        client = load_s3_file(f"clients/{client_code}.md")
        if client:
            ctx["client"] = client
        else:
            print(f"  No client file found for: {client_code}")

        keywords = load_s3_file(f"keywords/{client_code}_keywords.md")
        if keywords:
            ctx["keywords"] = keywords

        # Load visibility/ranking report if available
        visibility = load_s3_file(f"visibility/{client_code}_visibility.md")
        if not visibility:
            # Try CSV fallback
            visibility = load_s3_file(f"visibility/{client_code}_visibility.csv")
        if visibility:
            ctx["visibility"] = visibility
            print(f"  Visibility report loaded")

    # For copywriting and AEO tasks, find and fetch the relevant page
    if category in ("copywriting", "aeo") and todo and ctx.get("client"):
        page_url = find_page_url(todo, ctx["client"])
        if page_url:
            print(f"  Fetching current page: {page_url}")
            page_content = fetch_page_content(page_url)
            if page_content:
                ctx["current_page_url"] = page_url
                ctx["current_page_content"] = page_content
                print(f"  Page fetched ({len(page_content)} chars)")
        else:
            print(f"  No matching page URL found in client file")

    return ctx

# ── Anthropic API ──────────────────────────────────────────────────────────────

def call_claude(todo, category, context, creds):
    """Call the Anthropic API with task + context and return the output."""
    import urllib.request

    system_parts = []

    if "standards" in context:
        system_parts.append(
            f"# Copywriting & SEO Standards\n\n{context['standards']}"
        )
    if "client" in context:
        system_parts.append(
            f"# Client Context\n\n{context['client']}"
        )
    if "keywords" in context:
        system_parts.append(
            f"# Target Keywords\n\n{context['keywords']}"
        )
    if "visibility" in context:
        system_parts.append(
            f"# Keyword Visibility & Ranking Report\n\n{context['visibility']}"
        )
    if "current_page_url" in context:
        system_parts.append(
            f"# Current Page Content (for audit)\n\n"
            f"URL: {context['current_page_url']}\n\n"
            f"{context['current_page_content']}"
        )

    system_prompt = "\n\n---\n\n".join(system_parts) if system_parts else (
        "You are an SEO specialist assistant helping complete digital marketing tasks "
        "for dental practices."
    )

    user_message = f"""You have been assigned the following task in Basecamp:

Project: {todo['project_name']}
Task List: {todo['todolist_title']}
Task Title: {todo['title']}
Due Date: {todo['due_on'] or 'No due date'}
Task Category: {category}

Task Description:
{todo['description'] or 'No additional description provided.'}

---

PRE-WRITING INSTRUCTIONS — complete these steps before writing any copy:

If this is a copywriting, AEO, or SEO optimization task and current page content
has been provided above, begin with a brief audit before writing:

CURRENT PAGE AUDIT
URL: [url]
Current Meta Title: [title] — [Pass/Fail: 50-60 chars, keyword near front]
Current Meta Description: [description] — [Pass/Fail: 140-160 chars, has CTA]
Current H1: [h1] — [Pass/Fail: includes primary keyword, benefit-led]
Answer Capsule Under H1: [Present/Missing] — [assessment]
Primary Keyword Presence: [keyword] — [found in H1/first 100 words/H2/meta: yes/no]
FAQ Section: [Present/Missing] — [count of questions if present]
FAQPage Schema: [Present/Missing]
AEO Readiness Score: [X/10]
Overall SEO Score: [X/10]

Key Issues Found:
[List the 3-5 most impactful problems with the current page]

Then proceed to write the full replacement copy addressing all identified issues.

If no current page content was provided, skip the audit and proceed directly to writing.

---

FORMATTING RULES — follow these exactly:

1. Plain text output only. No markdown. No asterisks, no pound signs, no hyphens
   used as bullets, no underscores. Bold is indicated in the CMS fields themselves,
   not in your output.

2. For internal links, write them inline as: [link to /services/page-slug here]
   Example: "Learn more about our [link to /services/sedation-dentistry here]."

3. For editorial notes and CMS instructions, write them in square brackets on their
   own line: [Keep existing header image - no changes needed] or [Reviews widget here]
   or [Call CTA button here].

4. Match the output format to the client's editor type as specified in the client
   context file:
   - Classic Editor: full HTML with tags and shortcodes
   - Gutenberg: block-ready HTML
   - Elementor / WPBakery: plain text organized by section label
   - Webflow: output by CMS field name

5. Include FAQPage JSON-LD schema with script tags after the FAQ questions.

6. If the task category is "unknown", use your best judgment to determine what type
   of SEO or marketing task this is and complete it accordingly. State your
   interpretation at the top of the output in square brackets:
   [Interpreted as: copywriting / analysis / technical / etc.]

7. If the task involves multiple pages or items, complete the first one fully
   before moving to the next. Do not attempt to compress or summarize to fit
   everything in. Quality over quantity — one complete page is more useful
   than nine incomplete ones.

8. At the very end of your complete output, on its own line, write exactly:
   [END OF DRAFT]
   This confirms the full output was delivered. Do not write this until you are
   truly finished with all content.

Please complete this task using the standards and client context provided.
If information needed to complete the task is missing, flag it clearly
in square brackets rather than guessing: [MISSING: client phone number]
"""

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": creds["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
        return result["content"][0]["text"]

# ── DynamoDB State ─────────────────────────────────────────────────────────────

def is_seen(todo_id):
    response = table.get_item(Key={"todo_id": str(todo_id)})
    return "Item" in response

def mark_seen(todo_id):
    table.put_item(Item={
        "todo_id": str(todo_id),
        "seen_at": datetime.utcnow().isoformat(),
    })

# ── Email via SES ──────────────────────────────────────────────────────────────

def send_notification_email(todo, category, creds):
    ses = boto3.client("ses", region_name="us-east-1")
    to_email = creds["NOTIFICATION_EMAIL"]
    subject = f"[Basecamp Agent] New {category.title()} Task: {todo['title'][:50]}"

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; color: #1a1a1a; }}
  .card {{ background: #ffffff; border-radius: 8px; max-width: 640px; margin: 0 auto;
           padding: 32px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  .badge {{ display: inline-block; background: #f0f0f0; border-radius: 4px;
            font-size: 12px; font-weight: 600; letter-spacing: 0.05em;
            padding: 4px 10px; text-transform: uppercase; color: #555; margin-bottom: 20px; }}
  h2 {{ font-size: 20px; font-weight: 700; margin: 0 0 20px 0; color: #1a1a1a; }}
  .meta {{ border-top: 1px solid #efefef; border-bottom: 1px solid #efefef;
           padding: 16px 0; margin: 20px 0; }}
  .meta-row {{ display: flex; margin-bottom: 8px; font-size: 14px; }}
  .meta-label {{ font-weight: 600; width: 80px; color: #555; flex-shrink: 0; }}
  .meta-value {{ color: #1a1a1a; }}
  .description {{ font-size: 14px; line-height: 1.6; color: #444;
                  background: #f9f9f9; border-radius: 6px; padding: 16px; margin: 20px 0; }}
  .cta {{ margin-top: 24px; }}
  .cta a {{ background: #1a1a1a; color: #ffffff; text-decoration: none;
             border-radius: 6px; padding: 10px 20px; font-size: 14px; font-weight: 600; }}
  .footer {{ text-align: center; font-size: 12px; color: #aaa; margin-top: 32px; }}
  .notice {{ font-size: 13px; color: #888; font-style: italic; margin-top: 16px; }}
</style>
</head>
<body>
<div class="card">
  <div class="badge">{category}</div>
  <h2>{todo['title']}</h2>
  <div class="meta">
    <div class="meta-row"><span class="meta-label">Project</span><span class="meta-value">{todo['project_name']}</span></div>
    <div class="meta-row"><span class="meta-label">List</span><span class="meta-value">{todo['todolist_title']}</span></div>
    <div class="meta-row"><span class="meta-label">Due</span><span class="meta-value">{todo['due_on'] or 'No due date'}</span></div>
  </div>
  {f'<div class="description">{todo["description"]}</div>' if todo.get("description") else ''}
  <div class="cta"><a href="{todo['app_url']}">View in Basecamp →</a></div>
  <p class="notice">This task was flagged for manual review. Basecamp Agent did not attempt to complete it automatically.</p>
</div>
<div class="footer">Basecamp Agent</div>
</body>
</html>
"""

    ses.send_email(
        Source=to_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Html": {"Data": html},
                "Text": {"Data": f"{todo['title']}\n\nProject: {todo['project_name']}\nList: {todo['todolist_title']}\nDue: {todo['due_on'] or 'No due date'}\nURL: {todo['app_url']}"},
            },
        }
    )


def send_output_email(todo, category, output, creds):
    ses = boto3.client("ses", region_name="us-east-1")
    to_email = creds["NOTIFICATION_EMAIL"]
    subject = f"[Basecamp Agent] Draft Ready: {todo['title'][:50]}"

    print(f"  Output length: {len(output)} chars (limit: {EMAIL_SIZE_LIMIT})")

    # Check if output was fully delivered
    complete = "[END OF DRAFT]" in output
    if not complete:
        print(f"  WARNING: Output may be truncated - [END OF DRAFT] marker not found")

    # Always upload to S3 so full draft is always available
    s3_url = None
    display_output = output
    try:
        s3_url = upload_draft_to_s3(todo, output, creds)
        print(f"  Draft uploaded to S3")
    except Exception as e:
        print(f"  S3 upload failed ({type(e).__name__}): {e}")

    # Truncate display output for email if needed
    if len(output) > EMAIL_SIZE_LIMIT:
        display_output = output[:EMAIL_SIZE_LIMIT]
        print(f"  Email truncated to {EMAIL_SIZE_LIMIT} chars")

    # Convert plain text output to HTML
    html_output = ""
    in_script = False
    script_buffer = ""

    for line in display_output.split("\n"):
        stripped = line.strip()

        if "<script" in stripped:
            in_script = True
            script_buffer = stripped + "\n"
            continue

        if in_script:
            script_buffer += stripped + "\n"
            if "</script>" in stripped:
                in_script = False
                escaped = script_buffer.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                html_output += f'<pre style="background:#f4f4f4;padding:16px;border-radius:6px;font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-word">{escaped}</pre>'
                script_buffer = ""
            continue

        if not stripped:
            html_output += "<br>"
        elif stripped.startswith("BOLD:"):
            html_output += f"<p><strong>{stripped[5:].strip()}</strong></p>"
        elif stripped.isupper() and len(stripped) > 3:
            html_output += f"<h3>{stripped}</h3>"
        elif stripped.startswith("Q:"):
            html_output += f"<p><strong>{stripped}</strong></p>"
        elif stripped.startswith("A:"):
            html_output += f"<p style='margin-left:16px'>{stripped}</p>"
        elif stripped.startswith("[") and stripped.endswith("]"):
            html_output += f"<p style='color:#888;font-style:italic'>{stripped}</p>"
        else:
            html_output += f"<p>{stripped}</p>"

    # S3 and completeness banner
    truncated = len(output) > EMAIL_SIZE_LIMIT
    if s3_url or not complete:
        if not complete:
            banner_bg = "#fdecea"
            banner_border = "#f5c6cb"
            banner_color = "#721c24"
            banner_label = "Warning: output may be incomplete"
        elif truncated:
            banner_bg = "#fff8e1"
            banner_border = "#ffe082"
            banner_color = "#7a5c00"
            banner_label = "Draft truncated in email — full version saved to S3"
        else:
            banner_bg = "#e8f5e9"
            banner_border = "#a5d6a7"
            banner_color = "#1b5e20"
            banner_label = "Full draft saved to S3"

        s3_link = f'<a href="{s3_url}" style="color:#1a1a1a;font-weight:600;margin-left:8px;">Download full draft →</a><span style="color:#aaa;font-size:11px;margin-left:8px;">(link expires in 7 days)</span>' if s3_url else ""

        s3_banner = f"""
        <div style="background:{banner_bg};border:1px solid {banner_border};border-radius:6px;
                    padding:14px 18px;margin-bottom:24px;font-size:13px;color:{banner_color};">
            <strong>{banner_label}</strong>{s3_link}
        </div>
        """
    else:
        s3_banner = ""

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; color: #1a1a1a; }}
  .card {{ background: #ffffff; border-radius: 8px; max-width: 720px; margin: 0 auto;
           padding: 32px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  .badge {{ display: inline-block; background: #d4edda; border-radius: 4px;
            font-size: 12px; font-weight: 600; letter-spacing: 0.05em;
            padding: 4px 10px; text-transform: uppercase; color: #276749; margin-bottom: 20px; }}
  h2 {{ font-size: 20px; font-weight: 700; margin: 0 0 4px 0; color: #1a1a1a; }}
  h3 {{ font-size: 13px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
        color: #888; margin: 28px 0 8px 0; border-top: 1px solid #efefef; padding-top: 16px; }}
  .meta {{ font-size: 13px; color: #888; margin-bottom: 24px; }}
  .meta span {{ margin-right: 16px; }}
  .divider {{ border: none; border-top: 1px solid #efefef; margin: 24px 0; }}
  .output {{ font-size: 14px; line-height: 1.7; color: #2a2a2a; }}
  .output p {{ margin: 0 0 12px 0; }}
  .output strong {{ color: #1a1a1a; }}
  .output pre {{ white-space: pre-wrap; word-break: break-word; }}
  .cta {{ margin-top: 28px; padding-top: 20px; border-top: 1px solid #efefef; }}
  .cta a {{ background: #1a1a1a; color: #ffffff; text-decoration: none;
             border-radius: 6px; padding: 10px 20px; font-size: 14px; font-weight: 600; }}
  .cta a + a {{ margin-left: 12px; background: #444; }}
  .notice {{ font-size: 12px; color: #aaa; margin-top: 16px; }}
  .footer {{ text-align: center; font-size: 12px; color: #aaa; margin-top: 32px; }}
</style>
</head>
<body>
<div class="card">
  <div class="badge">Draft Ready — {category}</div>
  <h2>{todo['title']}</h2>
  <div class="meta">
    <span>📁 {todo['project_name']}</span>
    <span>📋 {todo['todolist_title']}</span>
    <span>📅 {todo['due_on'] or 'No due date'}</span>
  </div>
  <hr class="divider">
  {s3_banner}
  <div class="output">
    {html_output}
  </div>
  <div class="cta">
    <a href="{todo['app_url']}">View in Basecamp →</a>
    {f'<a href="{s3_url}">Full Draft (S3) →</a>' if s3_url else ''}
    <p class="notice">Review this draft before publishing. Mark the Basecamp task complete when done.</p>
  </div>
</div>
<div class="footer">Basecamp Agent</div>
</body>
</html>
"""

    print(f"  HTML email size: {len(html)} chars")

    ses.send_email(
        Source=to_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Html": {"Data": html},
                "Text": {"Data": display_output + ("\n\n[Full draft: " + s3_url + "]" if s3_url else "")},
            },
        }
    )
    print(f"  Draft email sent for: {todo['title'][:60]}")


# ── Lambda Handler ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print(f"\n{'='*50}")
    print(f"Lambda run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*50}")

    # Distributed lock — prevent concurrent executions from double-processing
    lock_key = "LAMBDA_LOCK"
    try:
        table.put_item(
            Item={"todo_id": lock_key, "seen_at": datetime.utcnow().isoformat()},
            ConditionExpression="attribute_not_exists(todo_id)"
        )
        print("Lock acquired")
    except Exception:
        print("Another instance is running. Exiting.")
        return {"statusCode": 200, "body": "Skipped — lock held by another instance"}

    try:
        creds = get_credentials()
        print("Credentials loaded")

        token = bc.get_access_token(
            creds["BASECAMP_CLIENT_ID"],
            creds["BASECAMP_CLIENT_SECRET"],
            creds["BASECAMP_REFRESH_TOKEN"],
        )
        print("Token refreshed OK")

        todos = bc.get_my_todos(
            token,
            creds["BASECAMP_ACCOUNT_ID"],
            creds["BASECAMP_USER_ID"],
            creds.get("USER_AGENT", "BasecampAgent (agent@example.com)"),
        )
        print(f"Total todos assigned to me: {len(todos)}")

        new_count = 0
        processed_count = 0

        for todo in todos:
            if is_seen(todo["id"]):
                continue

            category = classify(todo)
            client_code = extract_client_code(todo["project_name"])

            print(f"\n  NEW: [{category}] {todo['project_name']} - {todo['title'][:50]}")

            if category in SKIP_CATEGORIES:
                try:
                    send_notification_email(todo, category, creds)
                    print(f"  Notification email sent")
                except Exception as e:
                    print(f"  Email failed: {e}")
            else:
                context_data = load_context(category, client_code, todo=todo)
                try:
                    output = call_claude(todo, category, context_data, creds)
                    send_output_email(todo, category, output, creds)
                    print(f"  Draft email sent ({category})")
                    processed_count += 1
                except Exception as e:
                    print(f"  Claude call failed: {e}")
                    try:
                        send_notification_email(todo, category, creds)
                        print(f"  Fallback notification sent")
                    except Exception as e2:
                        print(f"  Fallback email also failed: {e2}")

            mark_seen(todo["id"])
            new_count += 1

        print(f"\nDone. {new_count} new tasks, {processed_count} drafts generated.")
        return {
            "statusCode": 200,
            "body": f"{new_count} new tasks, {processed_count} drafts generated"
        }

    finally:
        table.delete_item(Key={"todo_id": lock_key})
        print("Lock released")