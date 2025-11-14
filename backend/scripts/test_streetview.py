import requests
from PIL import Image
from io import BytesIO
import os

GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

# Test with Seagram Building
lat, lng = 40.7584563, -73.9721482

url = "https://maps.googleapis.com/maps/api/streetview"
params = {
    'location': f'{lat},{lng}',
    'size': '640x640',
    'fov': 120,
    'heading': 0,
    'pitch': 0,
    'key': GOOGLE_API_KEY
}

print(f"Fetching: {url}?{requests.compat.urlencode(params)}")
response = requests.get(url, params=params)
print(f"Status: {response.status_code}")
print(f"Content-Type: {response.headers.get('Content-Type')}")
print(f"Content length: {len(response.content)} bytes")

# Save to test
with open('test_streetview.jpg', 'wb') as f:
    f.write(response.content)
print("Saved to test_streetview.jpg")

# Try to open
try:
    img = Image.open(BytesIO(response.content))
    print(f"Image size: {img.size}")
    print(f"Image mode: {img.mode}")
    print("SUCCESS!")
except Exception as e:
    print(f"ERROR: {e}")
