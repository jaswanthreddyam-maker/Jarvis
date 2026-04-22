import requests
import json

file_path = 'Jarvis_Source_Fixed.zip'
url = 'https://file.io'

with open(file_path, 'rb') as f:
    response = requests.post(url, files={'file': f})
    
try:
    data = response.json()
    print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Failed to decode JSON: {e}")
    print(f"Response status: {response.status_code}")
    print(f"Response text: {response.text}")
