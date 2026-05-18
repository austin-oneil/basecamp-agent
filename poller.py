import requests
import json
import os
from datetime import datetime
import config
from tqdm import tqdm

# - Token Management -------

def get_access_token():
    response = requests.post(
        "https://launchpad.37signals.com/authorization/token",
        data={
            "type": "refresh",
            "refresh_token": config.BASECAMP_REFRESH_TOKEN,
            "client_id": config.BASECAMP_CLIENT_ID,
            "client_secret": config.BASECAMP_CLIENT_SECRET,
            "redirect_uri": "https://example.com/auth",
        }
    )
    response.raise_for_status()
    return response.json()["access_token"]

# - Basecamp API -------

def bc_get(url, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": config.USER_AGENT,
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_todos_from_list(todos_url, token, project_name, project_id, todolist_title):
    """Get all incomplete todos assigned to me from a single todos_url."""
    results = []
    try:
        todos = bc_get(todos_url, token)
    except Exception as e:
        print(f"    Error fetching todos: {e}")
        return results

    for todo in todos:
        if todo.get("completed"):
            continue
        assignees = todo.get("assignees", [])
        assigned_to_me = any(
            a["id"] == int(config.BASECAMP_USER_ID)
            for a in assignees
        )
        if assigned_to_me:
            results.append({
                "id": todo["id"],
                "title": todo["title"],
                "description": todo.get("description", ""),
                "due_on": todo.get("due_on"),
                "created_at": todo["created_at"],
                "project_name": project_name,
                "project_id": project_id,
                "todolist_title": todolist_title,
                "app_url": todo["app_url"],
            })
    return results


def get_my_todos(token):
    """
    Traverse all projects -> todosets -> todolists -> groups -> todos
    and return only incomplete todos assigned to me.
    """
    my_todos = []
    base = f"https://3.basecampapi.com/{config.BASECAMP_ACCOUNT_ID}"

    # Get all projects (handle pagination)
    projects = []
    url = f"{base}/projects.json"
    while url:
        response = requests.get(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": config.USER_AGENT,
        })
        response.raise_for_status()
        projects.extend(response.json())
        url = response.links.get("next", {}).get("url")

    print(f"Found {len(projects)} projects")

    for project in tqdm(projects, desc="Scanning projects", unit="projects"):
        project_id = project["id"]
        project_name = project["name"]

        # Find the primary todoset in this project's dock
        todoset = next(
            (d for d in project.get("dock", [])
             if d["name"] == "todoset" and d["enabled"]),
            None
        )
        if not todoset:
            continue

        # Get todolists
        try:
            todoset_data = bc_get(todoset["url"], token)
            todolists_url = todoset_data.get("todolists_url")
            if not todolists_url:
                continue
            todolists = bc_get(todolists_url, token)
        except Exception as e:
            print(f"  Skipping {project_name}: {e}")
            continue

        for todolist in todolists:
            if todolist.get("completed"):
                continue

            todolist_title = todolist.get("title", "")

            # Check todos directly on the todolist
            if todolist.get("todos_url"):
                my_todos.extend(get_todos_from_list(
                    todolist["todos_url"], token,
                    project_name, project_id, todolist_title
                ))

            # Check groups (sub-todolists) within this todolist
            if todolist.get("groups_url"):
                try:
                    groups = bc_get(todolist["groups_url"], token)
                    for group in groups:
                        if group.get("completed"):
                            continue
                        if group.get("todos_url"):
                            my_todos.extend(get_todos_from_list(
                                group["todos_url"], token,
                                project_name, project_id,
                                f"{todolist_title} > {group.get('title', '')}"
                            ))
                except Exception as e:
                    print(f"    Error fetching groups for {todolist_title}: {e}")

    return my_todos


# ── State Management ───────────────────────────────────────────────────────────

SEEN_FILE = os.path.expanduser("~/basecamp-agent/seen_todos.json")


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE) as f:
        return set(json.load(f))


def save_seen(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen_ids), f)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'=' * 50}")
    print(f"Poller run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    token = get_access_token()
    print("Token refreshed OK")

    seen_ids = load_seen()
    print(f"Previously seen todos: {len(seen_ids)}")

    todos = get_my_todos(token)
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