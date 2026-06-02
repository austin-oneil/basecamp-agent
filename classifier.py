import re

# ── Keyword Rules ──────────────────────────────────────────────────────────────
# Each category has a list of patterns checked against the lowercased todo title.
# Order matters: more specific categories are checked first.

RULES = [
    (
        "misc",
        [
            "action items are complete",
            "ensure all action items",
            "check in 30 days",
            "check in 60 days",
        ]
    ),
    (
        "aeo",
        [
            "ai engine optimization",
            "ai assessment",
            "aeo",
            "answer engine",
            "ai visibility",
        ]
    ),
    (
        "tracking",
        [
            "gtm",
            "adroll",
            "convirza",
            "tracking",
            "pixel",
            "tag manager",
            "unique audience",
            "invoca",
        ]
    ),
    (
        "review_collection",
        [
            "reviews to",
            "testimonies from reviews",
            "google reviews",
            "add reviews",
            "review content",
        ]
    ),
    (
        "technical",
        [
            "site speed",
            "speed requirements",
            "redirect",
            "frontend",
            "bug",
            "fix ",
            "broken",
            "404",
            "ssl",
            "crawl",
        ]
    ),
    (
        "page_creation",
        [
            "launch the new page",
            "launch new page",
            "create the page",
            "new page on the website",
            "publish",
        ]
    ),
    (
        "copywriting",
        [
            "copywriting",
            "copy for",
            "write copy",
            "spruce up copy",
            "refresh copy",
            "rewrite",
            "re-write",
            "copy on",
            "content for",
            "overhaul",
            "homepage",
            "service page",
            "refresh seo",
            "review/refresh",
            "beef up content",
            "improve pages",
            "improve visibility",
            "improve areas served",
            "update copy",
        ]
    ),
    (
        "analysis",
        [
            "keyword research",
            "kw research",
            "keyword gap",
            "marketing analysis",
            "marketing eval",
            "seo report",
            "keyword ranks",
            "tsa notes",
            "research",
        ]
    ),
]


def classify(todo):
    """
    Classify a todo dict into a task category.
    Returns a string category name.
    """
    title = todo.get("title", "").lower()
    description = todo.get("description", "").lower()
    combined = f"{title} {description}"

    for category, patterns in RULES:
        for pattern in patterns:
            if pattern in combined:
                return category

    return "unknown"


if __name__ == "__main__":
    # Quick test against some real task titles
    test_todos = [
        {"title": "BD SEO: Spruce up copy on dental crowns & bridges page", "description": ""},
        {"title": "WD: AdRoll Unique Audience IDs - Need to be Added Again to GTM", "description": ""},
        {"title": "MHLD SEO: Run AI assessment (TMJ, sedation, implants)", "description": ""},
        {"title": "KEW SEO: Add relevant testimonies from reviews to service pages", "description": ""},
        {"title": "RGA SEO: Launch the new page on the website after client approval", "description": ""},
        {"title": "SKDO SEO: Complete detailed KW research", "description": ""},
        {"title": "BD SEO: Ensure All Action Items Have Been Completed (Check in 30 days, then 60 if needed)", "description": ""},
        {"title": "Make sure site speed satisfies speed requirements", "description": ""},
        {"title": "SEO: Create copywriting for the new page", "description": ""},
        {"title": "FOD SEO: Improve visibility for Billerica, Dracut and North Andover", "description": ""},
    ]

    for todo in test_todos:
        category = classify(todo)
        print(f"  [{category:20s}] {todo['title'][:70]}")