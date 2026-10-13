import os
import json
import subprocess
import urllib.request
from base64 import b64encode
from dotenv import load_dotenv
from nacl import encoding, public

load_dotenv()

def get_git_token():
    p = subprocess.Popen(['git', 'credential', 'fill'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate('protocol=https\nhost=github.com\n\n')
    for line in out.splitlines():
        if line.startswith('password='):
            return line.split('=', 1)[1]
    raise RuntimeError("Could not retrieve GitHub token from git credential helper")

def encrypt_secret(public_key_str: str, secret_value: str) -> str:
    pub_key = public.PublicKey(public_key_str.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")

def upload_secret(owner: str, repo: str, token: str, secret_name: str, secret_val: str):
    # 1. Fetch public key
    pk_url = f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key"
    req = urllib.request.Request(pk_url, headers={"Authorization": f"Bearer {token}", "User-Agent": "Python"})
    with urllib.request.urlopen(req) as resp:
        pk_data = json.loads(resp.read())
    
    key_id = pk_data["key_id"]
    public_key = pk_data["key"]
    
    # 2. Encrypt
    encrypted_value = encrypt_secret(public_key, secret_val)
    
    # 3. Upload
    put_url = f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}"
    payload = json.dumps({"encrypted_value": encrypted_value, "key_id": key_id}).encode()
    put_req = urllib.request.Request(
        put_url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Python", "Content-Type": "application/json"},
        method="PUT"
    )
    with urllib.request.urlopen(put_req) as resp:
        print(f"Secret {secret_name} uploaded successfully (HTTP {resp.status})")

def main():
    token = get_git_token()
    owner = "barshal-horse"
    repo = "technical-trading"
    
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not alpaca_key or not alpaca_secret:
        raise ValueError("ALPACA_API_KEY or ALPACA_SECRET_KEY not found in local .env!")
        
    upload_secret(owner, repo, token, "ALPACA_API_KEY", alpaca_key)
    upload_secret(owner, repo, token, "ALPACA_SECRET_KEY", alpaca_secret)
    print("All secrets synchronized to GitHub repository successfully!")

if __name__ == "__main__":
    main()
