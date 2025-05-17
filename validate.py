import os
import httpx
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BROWSERBASE_API_KEY")
project_id = os.getenv("BROWSERBASE_PROJECT_ID")

def validate_browserbase_credentials():
    print("🧪 Validating Browserbase credentials...")
    
    if not api_key:
        print("❌ Missing BROWSERBASE_API_KEY in .env")
        return
    
    if not project_id:
        print("❌ Missing BROWSERBASE_PROJECT_ID in .env")
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {"projectId": project_id}

    try:
        response = httpx.post(
            "https://api.browserbase.com/v1/sessions",
            headers=headers,
            json=payload
        )
        print(f"📡 Status code: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print("✅ Successfully authenticated with Browserbase.")
            print(f"🌐 WebSocket URL: {data.get('wsUrl', '[missing]')}")
        else:
            print("❌ Failed to authenticate.")
            print("🔍 Response body:", response.text)

    except Exception as e:
        print(f"💥 Unexpected error: {e}")

if __name__ == "__main__":
    validate_browserbase_credentials()
