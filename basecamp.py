import requests

def get_access_token(client_id, client_secret, refresh_token, redirect_uri="https://example.com/auth"):
    """Exchange a refresh token for a fresh access token."""
    response = requests.post(
        "https://launchpad.37signals.com/authorization/token",
        data={
            "type": "refresh",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    )
    response.raise_for_status()
    return response.json()["access_token"]


def bc_get(url, token, user_agent):
    """Make an authenticated GET request to the Basecamp API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent,
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json(), response.links


def get_todos_from_list(todos_url, token, user_agent, user_id, project_name, project_id, todolist_title):
    """Get all incomplete todos assigned to user_id from a single todos_url."""
    results = []
    try:
        todos, _ = bc_get(todos_url, token, user_agent)
    except Exception as e:
        print(f"    Error fetching todos: {e}")
        return results

    for todo in todos:
        if todo.get("completed"):
            continue
        assignees = todo.get("assignees", [])
        assigned_to_me = any(a["id"] == int(user_id) for a in assignees)
        if assigned_to_me:
            results.append({
                "id": str(todo["id"]),
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


def get_my_todos(token, account_id, user_id, user_agent):
    """
    Traverse all projects -> todosets -> todolists -> groups -> todos
    and return only incomplete todos assigned to user_id.
    """
    my_todos = []
    base = f"https://3.basecampapi.com/{account_id}"

    # Paginate through all projects
    projects = []
    url = f"{base}/projects.json"
    while url:
        response = requests.get(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
        })
        response.raise_for_status()
        projects.extend(response.json())
        url = response.links.get("next", {}).get("url")

    print(f"Found {len(projects)} projects")

    for project in projects:
        project_id = project["id"]
        project_name = project["name"]

        todoset = next(
            (d for d in project.get("dock", [])
             if d["name"] == "todoset" and d["enabled"]),
            None
        )
        if not todoset:
            continue

        try:
            todoset_data, _ = bc_get(todoset["url"], token, user_agent)
            todolists_url = todoset_data.get("todolists_url")
            if not todolists_url:
                continue
            todolists = []
            while todolists_url:
                tl_response = requests.get(todolists_url, headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": user_agent,
                })
                tl_response.raise_for_status()
                todolists.extend(tl_response.json())
                todolists_url = tl_response.links.get("next", {}).get("url")
        except Exception as e:
            print(f"  Skipping {project_name}: {e}")
            continue

        for todolist in todolists:
            if todolist.get("completed"):
                continue

            todolist_title = todolist.get("title", "")

            if todolist.get("todos_url"):
                my_todos.extend(get_todos_from_list(
                    todolist["todos_url"], token, user_agent, user_id,
                    project_name, project_id, todolist_title
                ))

            if todolist.get("groups_url"):
                try:
                    groups, _ = bc_get(todolist["groups_url"], token, user_agent)
                    for group in groups:
                        if group.get("completed"):
                            continue
                        if group.get("todos_url"):
                            my_todos.extend(get_todos_from_list(
                                group["todos_url"], token, user_agent, user_id,
                                project_name, project_id,
                                f"{todolist_title} > {group.get('title', '')}"
                            ))
                except Exception as e:
                    print(f"    Error fetching groups for {todolist_title}: {e}")

    return my_todos