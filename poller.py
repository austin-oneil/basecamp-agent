import json
import os
from datetime import datetime
from tqdm import tqdm
import config
import basecamp as bc

SEEN_FILE = os.path.expanduser("~/basecamp-agent/seen_todos.json")

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE) as f:
        return set(json.load(f))

def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen_ids), f)

def main():
    print(f"\n{'='*50}")
    print(f"Poller run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    token = bc.get_access_token(
        config.BASECAMP_CLIENT_ID,
        config.BASECAMP_CLIENT_SECRET,
        config.BASECAMP_REFRESH_TOKEN,
    )
    print("Token refreshed OK")

    seen_ids = load_seen()
    print(f"Previously seen todos: {len(seen_ids)}")

    todos = bc.get_my_todos(
        token,
        config.BASECAMP_ACCOUNT_ID,
        config.BASECAMP_USER_ID,
        config.USER_AGENT,
    )
    print(f"Current todos assigned to me: {len(todos)}")

    new_todos = [t for t in todos if str(t["id"]) not in seen_ids]
    print(f"New todos to process: {len(new_todos)}")

    for todo in new_todos:
        print(f"\n  NEW TASK DETECTED:")
        print(f"  Project : {todo['project_name']}")
        print(f"  List    : {todo['todolist_title']}")
        print(f"  Title   : {todo['title']}")
        print(f"  Due     : {todo['due_on'] or 'No due date'}")
        print(f"  URL     : {todo['app_url']}")
        seen_ids.add(str(todo["id"]))

    save_seen(seen_ids)
    print(f"\nDone. Seen list updated to {len(seen_ids)} todos.")

if __name__ == "__main__":
    main()