import sys
import requests
import os

def mock_places_api_behavior():
    print("Testing mock API logic")
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        api_key = "dummy"
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": "鹿児島市 焼肉", "language": "ja", "key": api_key}
    
    try:
        r = requests.get(url, params=params, timeout=20)
        print(f"Status Code: {r.status_code}")
        data = r.json()
        print(f"Response Status: {data.get('status')}")
        print(f"Response Keys: {data.keys()}")
        if "error_message" in data:
            print(f"Error Msg: {data['error_message']}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    mock_places_api_behavior()
