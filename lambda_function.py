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

# ── Context Loading ────────────────────────────────────────────────────────────

def load_context(category, client_code):
    """Load standards doc, client file, and keyword file from S3."""
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

    FORMATTING RULES — follow these exactly:

    1. Plain text output only. No markdown. No asterisks, no pound signs, no hyphens
       used as bullets, no underscores. Bold is indicated in the CMS fields themselves,
       not in your output.

    2. For internal links, write them inline as: [link to /services/page-slug here]
       Example: "Learn more about our [link to /services/sedation-dentistry here]."

    3. For editorial notes and CMS instructions, write them in square brackets on their
       own line: [Keep existing header image - no changes needed] or [Reviews widget here]
       or [Call CTA button here].

    4. Follow the Webflow CMS output format exactly as defined in the client file.
       Every field must be labeled and separated clearly.

    5. Include FAQPage JSON-LD schema with script tags after the FAQ questions.

    6. If the task category is "unknown", use your best judgment to determine what type
       of SEO or marketing task this is and complete it accordingly. State your
       interpretation at the top of the output in square brackets:
       [Interpreted as: copywriting / analysis / technical / etc.]

    Please complete this task using the standards and client context provided.
    If information needed to complete the task is missing, flag it clearly
    in square brackets rather than guessing: [MISSING: client phone number]
    """

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
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

    # Convert plain text output to basic HTML
    # Bold lines that are all-caps labels like "BASIC INFO", "SEO FIELDS" etc.
    # Wrap paragraphs, preserve line breaks
    html_output = ""
    for line in output.split("\n"):
        stripped = line.strip()
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
        elif stripped.startswith("<script"):
            html_output += f"<pre style='background:#f4f4f4;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto'>{stripped}</pre>"
        else:
            html_output += f"<p>{stripped}</p>"

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
  <div class="output">
    {html_output}
  </div>
  <div class="cta">
    <a href="{todo['app_url']}">View in Basecamp →</a>
    <p class="notice">Review this draft before publishing. Mark the Basecamp task complete when done.</p>
  </div>
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
                "Text": {"Data": output},
            },
        }
    )
    print(f"  Draft email sent for: {todo['title'][:60]}")

# ── Lambda Handler ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print(f"\n{'='*50}")
    print(f"Lambda run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*50}")

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
            context_data = load_context(category, client_code)
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