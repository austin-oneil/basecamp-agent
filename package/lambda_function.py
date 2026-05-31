import json
import boto3
from datetime import datetime
import basecamp as bc

secretsmanager = boto3.client("secretsmanager", region_name="us-east-1")
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
table = dynamodb.Table("basecamp-seen-todos")

def get_credentials():
    secret = secretsmanager.get_secret_value(SecretId="basecamp-agent/credentials")
    return json.loads(secret["SecretString"])

def is_seen(todo_id):
    response = table.get_item(Key={"todo_id": str(todo_id)})
    return "Item" in response

def mark_seen(todo_id):
    table.put_item(Item={
        "todo_id": str(todo_id),
        "seen_at": datetime.utcnow().isoformat(),
    })

def send_email(todo, creds):
    ses = boto3.client("ses", region_name="us-east-1")
    to_email = creds["NOTIFICATION_EMAIL"]
    subject = f"[Basecamp Agent] New Task: {todo['title'][:60]}"
    body = f"""New task assigned to you in Basecamp:

Project:  {todo['project_name']}
List:     {todo['todolist_title']}
Title:    {todo['title']}
Due:      {todo['due_on'] or 'No due date'}
URL:      {todo['app_url']}

Description:
{todo['description'] or 'No description provided.'}

---
This notification was sent by your Basecamp Agent.
"""
    ses.send_email(
        Source=to_email,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }
    )
    print(f"  Email sent for: {todo['title'][:60]}")

def lambda_handler(event, context):
    print(f"\n{'='*50}")
    print(f"Lambda run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*50}")

    creds = get_credentials()
    print("Credentials loaded from Secrets Manager")

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
    for todo in todos:
        if not is_seen(todo["id"]):
            print(f"\n  NEW: {todo['project_name']} - {todo['title'][:60]}")
            try:
                send_email(todo, creds)
            except Exception as e:
                print(f"  Email failed: {e}")
            mark_seen(todo["id"])
            new_count += 1

    print(f"\nDone. {new_count} new tasks processed.")
    return {"statusCode": 200, "body": f"{new_count} new tasks processed"}