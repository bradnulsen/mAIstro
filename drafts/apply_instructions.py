import json
import urllib.request

# Read the source file
with open(r'c:\GIT\agentic\v7\drafts\job-instructions.md', 'r', encoding='utf-8') as f:
    content = f.read()

# Parse code blocks per job
jobs = ['strategist', 'designer', 'architect', 'engineer']
instructions = {}

for job in jobs:
    idx = content.lower().find(f'## {job}')
    if idx == -1:
        print(f'ERROR: heading not found for {job}')
        continue
    start = content.find('```\n', idx)
    if start == -1:
        print(f'ERROR: code block start not found for {job}')
        continue
    start += 4
    end = content.find('\n```', start)
    if end == -1:
        print(f'ERROR: code block end not found for {job}')
        continue
    instructions[job] = content[start:end]

base = 'http://localhost:8420/api/jobs'

def patch_job(job_id, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'{base}/{job_id}', data=data, method='PATCH')
    req.add_header('Content-Type', 'application/json')
    resp = urllib.request.urlopen(req)
    return resp.status, json.loads(resp.read().decode())

# Update instructions for all 4 jobs
for job_id in jobs:
    text = instructions[job_id]
    status, body = patch_job(job_id, {'instructions': text})
    # Verify
    actual = body.get('properties', {}).get('instructions', '')
    match = actual == text
    print(f'{job_id} instructions: {status} (match={match}, len={len(text)})')

# Update subscriptions for designer and architect
for job_id, subs in [('designer', ['STRATEGY.md', 'DESIGN.md']), ('architect', ['DESIGN.md', 'architecture/**'])]:
    status, body = patch_job(job_id, {'subscriptions': subs})
    actual = body.get('properties', {}).get('subscriptions', [])
    print(f'{job_id} subscriptions: {status} set to {actual}')

# Clear schedules for all 4 jobs
for job_id in jobs:
    status, body = patch_job(job_id, {'schedule': ''})
    actual = body.get('properties', {}).get('schedule', '')
    print(f'{job_id} schedule: {status} set to "{actual}"')

print('\nDone.')
