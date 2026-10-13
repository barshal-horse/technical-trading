import subprocess
import urllib.request
import json

def get_token():
    p = subprocess.Popen(['git', 'credential', 'fill'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate('protocol=https\nhost=github.com\n\n')
    for line in out.splitlines():
        if line.startswith('password='):
            return line.split('=', 1)[1]
    return None

token = get_token()
headers = {'User-Agent': 'Python'}
if token:
    headers['Authorization'] = f'Bearer {token}'

url = 'https://api.github.com/repos/barshal-horse/technical-trading/actions/runs?per_page=15'
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())

print(f"Total Workflow Runs: {data.get('total_count')}")
print(f"{'RUN ID':<13} {'STATUS':<12} {'CONCLUSION':<12} {'EVENT':<18} {'CREATED AT':<22}")
print("-" * 75)
for run in data.get('workflow_runs', [])[:6]:
    conclusion = str(run['conclusion'])
    print(f"{run['id']:<13} {run['status']:<12} {conclusion:<12} {run['event']:<18} {run['created_at']:<22}")
    try:
        url_job = f"https://api.github.com/repos/barshal-horse/technical-trading/actions/runs/{run['id']}/jobs"
        with urllib.request.urlopen(urllib.request.Request(url_job, headers=headers)) as j_resp:
            j_data = json.loads(j_resp.read())
        for j in j_data.get('jobs', []):
            print(f"   Job: {j['name']} (Started: {j.get('started_at')} -> Ended: {j.get('completed_at')})")
            for s in j.get('steps', []):
                if s.get('conclusion') not in ['success', None]:
                    print(f"      Step: {s['name']} -> {s.get('conclusion')}")
    except Exception as e:
        print(f"   Error fetching job: {e}")
