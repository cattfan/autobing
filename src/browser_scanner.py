import json
import urllib.request
import pathlib
import sys

def scan_profiles():
    # Attempt to read GPM API URL from settings
    url = "http://127.0.0.1:9495"
    config_path = pathlib.Path(__file__).parent.parent / "config" / "settings.json"
    
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                
                # Get Platform Context
                platform = settings.get("browser_type", "gpm")
                api_url = settings.get("browser_api_url", settings.get("gpm_api_url", "http://127.0.0.1:9495")).rstrip("/")
        except Exception:
            pass
            
    try:
        # Switch Route based on platform
        if platform == "genlogin":
            req_url = f"{api_url}/profiles"
        elif platform == "adspower":
            req_url = f"{api_url}/api/v1/user/list"
        elif platform == "dolphin":
            req_url = f"{api_url}/v1.0/browser_profiles"
        elif platform == "vmlogin":
            req_url = f"{api_url}/api/v1/profile/list"
        else: # gpm / default
            req_url = f"{api_url}/api/v1/profiles?page=1&per_page=500"

        req = urllib.request.Request(req_url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
            # Universal payload extractor
            profiles = []
            if isinstance(data, list):
                profiles = data
            elif isinstance(data, dict):
                # Search common keys for lists 
                if "data" in data and isinstance(data["data"], list):
                    profiles = data["data"]
                elif "data" in data and isinstance(data["data"], dict) and "list" in data["data"]:
                    profiles = data["data"]["list"]
                elif "data" in data and isinstance(data["data"], dict) and "data" in data["data"]:
                    profiles = data["data"]["data"]
                elif "list" in data and isinstance(data["list"], list):
                    profiles = data["list"]
                else:
                    profiles = data.get("data", [])
            
            # Dolphin uses "id", GPM/GenLogin use "id". AdsPower uses "user_id". VMLogin uses "profileId"
            # Normalize list for UI
            normalized = []
            for p in profiles:
                if isinstance(p, dict):
                    pid = p.get("id") or p.get("user_id") or p.get("profileId")
                    pname = p.get("name") or p.get("profileName") or f"Profile {pid}"
                    if pid:
                        normalized.append({"id": str(pid), "name": str(pname)})
                
            print(json.dumps(normalized))
            sys.exit(0)
    except Exception as e:
        print(json.dumps([{"id": "ERROR", "name": f"Failed to fetch: {str(e)}"}]))
        sys.exit(0)

    print(json.dumps([]))

if __name__ == "__main__":
    scan_profiles()
