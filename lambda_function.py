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
SKIP_CATEGORIES = {"misc", "unknown", "analysis", "page_creation"}

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
    context = {}

    standards = load_s3_file(f"standards/{category}.md")
    if standards:
        context["standards"] = standards
    else:
        print(f"  No standards doc found for category: {category}")

    if client_code:
        client = load_s3_file(f"clients/{client_code}.md")
        if client:
            context["client"] = client
        else:
            print(f"  No client file found for: {client_code}")

        keywords = load_s3_file(f"keywords/{client_code}_keywords.md")
        if keywords:
            context["keywords"] = keywords

    return context

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

**Project:** {todo['project_name']}
**Task List:** {todo['todolist_title']}
**Task Title:** {todo['title']}
**Due Date:** {todo['due_on'] or 'No due date'}
**Task Category:** {category}

**Task Description:**
{todo['description'] or 'No additional description provided.'}

---

Please complete this task to the best of your ability using the standards and client context 
provided. Produce a complete, ready-to-use output. If you need information that is not 
available in the context provided, flag it clearly rather than guessing.
"""

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
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
    """Send a simple notification email for tasks the agent doesn't process."""
    ses = boto3.client("ses", region_name="us-east-1")
    to_email = creds["NOTIFICATION_EMAIL"]
    subject = f"[Basecamp Agent] New {category.title()} Task: {todo['title'][:50]}"
    body = f"""New task assigned to you in Basecamp:

Project:   {todo['project_name']}
List:      {todo['todolist_title']}
Title:     {todo['title']}
Category:  {category}
Due:       {todo['due_on'] or 'No due date'}
URL:       {todo['app_url']}

Description:
{todo['description'] or 'No description provided.'}

---
This task was flagged for your manual review.
Basecamp Agent did not attempt to complete it automatically.
"""
    ses.send_email(
        Source=to_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }
    )


def send_output_email(todo, category, output, creds):
    """Send the agent's completed work output via email."""
    ses = boto3.client("ses", region_name="us-east-1")
    to_email = creds["NOTIFICATION_EMAIL"]
    subject = f"[Basecamp Agent] {category.title()} Draft Ready: {todo['title'][:50]}"
    body = f"""Basecamp Agent has completed the following task:

Project:   {todo['project_name']}
List:      {todo['todolist_title']}
Title:     {todo['title']}
Category:  {category}
Due:       {todo['due_on'] or 'No due date'}
URL:       {todo['app_url']}

---

{output}

---
Review this draft before publishing. Mark the Basecamp task complete when done.
"""
    ses.send_email(
        Source=to_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }
    )

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