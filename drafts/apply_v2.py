import urllib.request, json

def patch(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status

text = open("drafts/goal-descriptions-v2.md").read()

def extract(section_name, next_section=None):
    chunk = text.split("## " + section_name)[1]
    if next_section:
        chunk = chunk.split("## " + next_section)[0]
    parts = chunk.split("```")
    return parts[1].strip()

goals = {
    "strategist": {
        "description": "Receives human intent, produces precise product direction",
        "instructions": extract("Strategist", "Designer"),
    },
    "designer": {
        "description": "Translates strategic direction into behavioral specification",
        "instructions": extract("Designer", "Architect"),
    },
    "architect": {
        "description": "Translates behavioral specification into technical structure",
        "instructions": extract("Architect", "Engineer"),
    },
    "engineer": {
        "description": "Translates technical structure into working code",
        "instructions": extract("Engineer", None),
    },
}

base = "http://localhost:8420/api/goals"
for gid, props in goals.items():
    s = patch(f"{base}/{gid}", props)
    print(f"{gid}: {s}")
