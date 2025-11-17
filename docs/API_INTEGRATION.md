# NYC Scan API Integration Guide

Complete guide for integrating the NYC Scan backend API into your mobile app (React Native/Expo or native).

## Base URL

**Production (Modal):**
```
https://your-workspace--nyc-scan-api-fastapi-app.modal.run
```

**Local Development:**
```
http://localhost:8000
```

---

## 1. Health Check

Before making requests, verify the API is healthy:

```bash
curl https://api.nycscan.app/health
```

Response:
```json
{
  "status": "healthy",
  "timestamp": 1700123456.789,
  "checks": {
    "api": "ok",
    "clip_model": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

---

## 2. Scan Building - POST /api/scan

**Primary endpoint for identifying buildings from photos.**

### Request

```typescript
interface ScanRequest {
  photo: File;              // Image file from camera
  gps_lat: number;          // User's latitude (-90 to 90)
  gps_lng: number;          // User's longitude (-180 to 180)
  compass_bearing: number;  // Compass direction (0-360°, 0=North)
  phone_pitch: number;      // Phone angle from horizontal (-90 to 90)
  phone_roll?: number;      // Phone tilt left/right (optional)
  altitude?: number;        // Height in meters (optional)
  floor?: number;           // Estimated floor (optional)
  confidence?: number;      // Position confidence 0-100 (optional)
  movement_type?: string;   // 'stationary'|'walking'|'running' (optional)
  gps_accuracy?: number;    // GPS accuracy in meters (optional)
  user_id?: string;         // For tracking (optional)
}
```

### Example - TypeScript/React Native

```typescript
async function scanBuilding(
  photoFile: File,
  gpsData: {
    latitude: number;
    longitude: number;
    bearing: number;
    pitch: number;
    accuracy?: number;
  }
): Promise<ScanResponse> {
  const formData = new FormData();

  // Compress image before upload for better performance
  const compressedPhoto = await compressImage(photoFile, 1024, 0.85);

  formData.append('photo', compressedPhoto);
  formData.append('gps_lat', gpsData.latitude.toString());
  formData.append('gps_lng', gpsData.longitude.toString());
  formData.append('compass_bearing', gpsData.bearing.toString());
  formData.append('phone_pitch', gpsData.pitch.toString());

  if (gpsData.accuracy !== undefined) {
    formData.append('gps_accuracy', gpsData.accuracy.toString());
  }

  const response = await fetch(
    `${API_BASE_URL}/api/scan`,
    {
      method: 'POST',
      body: formData,
    }
  );

  if (!response.ok) {
    throw new Error(`Scan failed: ${response.statusText}`);
  }

  return response.json();
}
```

### Response

```json
{
  "scan_id": "uuid-string-here",
  "matches": [
    {
      "bin": "1234567",
      "address": "123 Main St, New York, NY 10001",
      "confidence": 0.95,
      "distance_meters": 45,
      "images_count": 12
    },
    {
      "bin": "1234568",
      "address": "125 Main St, New York, NY 10001",
      "confidence": 0.72,
      "distance_meters": 120
    }
  ],
  "show_picker": false,
  "processing_time_ms": 2150,
  "performance": {
    "upload_ms": 340,
    "geospatial_ms": 120,
    "reference_images_ms": 890,
    "clip_comparison_ms": 800
  },
  "debug_info": {
    "num_candidates": 45,
    "num_reference_images": 127,
    "num_matches": 3
  }
}
```

### Error Cases

**No candidates found:**
```json
{
  "scan_id": "uuid-string",
  "error": "no_candidates",
  "message": "No buildings found in your view. Try getting closer or adjusting your angle.",
  "matches": [],
  "processing_time_ms": 450
}
```

**No reference images available:**
```json
{
  "scan_id": "uuid-string",
  "error": "no_reference_images",
  "message": "No reference images available for these buildings. Our database is still growing!",
  "candidates": [
    {
      "bin": "1234567",
      "address": "123 Main St",
      "distance_meters": 50
    }
  ],
  "processing_time_ms": 890
}
```

---

## 3. Confirm Building - POST /api/scans/{scan_id}/confirm

**Endpoint for user confirmation of correct building.**

### Request

```typescript
interface ConfirmRequest {
  confirmed_bin: string;      // BIN of confirmed building
  confirmation_time_ms?: number; // Time taken to confirm
}
```

### Example

```typescript
async function confirmBuilding(
  scanId: string,
  confirmedBin: string
): Promise<void> {
  const formData = new FormData();
  formData.append('confirmed_bin', confirmedBin);

  const response = await fetch(
    `${API_BASE_URL}/api/scans/${scanId}/confirm`,
    {
      method: 'POST',
      body: formData,
    }
  );

  if (!response.ok) {
    throw new Error('Failed to confirm building');
  }

  return response.json();
}
```

### Response

```json
{
  "status": "confirmed",
  "scan_id": "uuid-string",
  "confirmed_bin": "1234567"
}
```

---

## 4. Submit Feedback - POST /api/scans/{scan_id}/feedback

**Endpoint for user feedback on scan accuracy.**

### Request

```typescript
interface FeedbackRequest {
  rating: number;              // 1-5 stars
  feedback_text?: string;      // Optional comment
  feedback_type?: string;      // 'correct'|'incorrect'|'slow'|'no_match'
}
```

### Example

```typescript
async function submitFeedback(
  scanId: string,
  rating: number,
  feedbackType: 'correct' | 'incorrect' | 'slow',
  comment?: string
): Promise<void> {
  const formData = new FormData();
  formData.append('rating', rating.toString());
  formData.append('feedback_type', feedbackType);

  if (comment) {
    formData.append('feedback_text', comment);
  }

  const response = await fetch(
    `${API_BASE_URL}/api/scans/${scanId}/feedback`,
    {
      method: 'POST',
      body: formData,
    }
  );

  if (!response.ok) {
    throw new Error('Failed to submit feedback');
  }

  return response.json();
}
```

---

## 5. Get Scan Details - GET /api/scans/{scan_id}

**Retrieve scan history and details.**

### Example

```typescript
async function getScanDetails(scanId: string): Promise<ScanDetails> {
  const response = await fetch(
    `${API_BASE_URL}/api/scans/${scanId}`
  );

  if (!response.ok) {
    throw new Error('Failed to fetch scan details');
  }

  return response.json();
}
```

---

## Helper Functions

### Image Compression

Compress images before upload to reduce bandwidth:

```typescript
async function compressImage(
  file: File,
  maxSize: number = 1024,
  quality: number = 0.85
): Promise<File> {
  return new Promise((resolve) => {
    const reader = new FileReader();

    reader.onload = (e) => {
      const img = new Image();

      img.onload = () => {
        const canvas = document.createElement('canvas');

        // Calculate new dimensions
        let width = img.width;
        let height = img.height;

        if (width > height && width > maxSize) {
          height = (height / width) * maxSize;
          width = maxSize;
        } else if (height > maxSize) {
          width = (width / height) * maxSize;
          height = maxSize;
        }

        canvas.width = width;
        canvas.height = height;

        const ctx = canvas.getContext('2d')!;
        ctx.drawImage(img, 0, 0, width, height);

        canvas.toBlob((blob) => {
          resolve(
            new File([blob!], file.name, {
              type: 'image/jpeg',
              lastModified: Date.now(),
            })
          );
        }, 'image/jpeg', quality);
      };

      img.src = e.target?.result as string;
    };

    reader.readAsDataURL(file);
  });
}
```

### Retry Logic with Exponential Backoff

```typescript
async function scanWithRetry(
  photoFile: File,
  gpsData: GpsData,
  maxRetries: number = 3
): Promise<ScanResponse> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await scanBuilding(photoFile, gpsData);
    } catch (error) {
      if (i === maxRetries - 1) throw error;

      // Exponential backoff: 1s, 2s, 4s
      const delayMs = 1000 * Math.pow(2, i);
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }

  throw new Error('Scan failed after max retries');
}
```

### React Native Camera Integration

```typescript
import { Camera } from 'expo-camera';
import * as Location from 'expo-location';
import { Magnetometer, Accelerometer } from 'expo-sensors';

async function captureAndScan(cameraRef: React.RefObject<Camera>) {
  // Get GPS location
  const location = await Location.getCurrentPositionAsync({
    accuracy: Location.Accuracy.BestForNavigation,
  });
  const { latitude, longitude, accuracy } = location.coords;

  // Get compass bearing
  const magnetometerData = await new Promise<MagnetometerReading>((resolve) => {
    const sub = Magnetometer.addListener((data) => {
      sub.remove();
      resolve(data);
    });
  });
  const bearing = Math.atan2(magnetometerData.y, magnetometerData.x) * (180 / Math.PI);

  // Get phone pitch from accelerometer
  const accelData = await new Promise<AccelerometerReading>((resolve) => {
    const sub = Accelerometer.addListener((data) => {
      sub.remove();
      resolve(data);
    });
  });
  const pitch = Math.asin(accelData.z) * (180 / Math.PI);

  // Capture photo
  const photo = await cameraRef.current?.takePictureAsync({
    quality: 0.8,
    base64: false,
  });

  if (!photo) throw new Error('Failed to capture photo');

  // Resize image
  const resizedPhoto = await resizeImage(photo.uri, 1024);

  // Scan building
  const result = await scanBuilding(resizedPhoto, {
    latitude,
    longitude,
    bearing: (bearing + 360) % 360, // Normalize to 0-360
    pitch,
    accuracy,
  });

  return result;
}
```

---

## Error Handling

All endpoints may return standard HTTP error codes:

| Status | Meaning | Handling |
|--------|---------|----------|
| 200 | Success | Process results |
| 400 | Bad Request | Check input validation (GPS bounds, etc) |
| 404 | Not Found | Scan ID or resource doesn't exist |
| 500 | Server Error | Use Sentry integration or retry with backoff |
| 503 | Service Unavailable | API is down or overloaded, retry |

### Complete Error Handling Example

```typescript
async function scanWithErrorHandling(
  photoFile: File,
  gpsData: GpsData
): Promise<ScanResponse> {
  try {
    return await scanWithRetry(photoFile, gpsData);
  } catch (error: any) {
    if (error.message.includes('no_candidates')) {
      // Show user message: "Move closer or adjust angle"
      console.warn('No buildings found in view');
      throw error;
    } else if (error.message.includes('no_reference_images')) {
      // Show user message: "Building not in database yet"
      console.warn('No images available');
      throw error;
    } else if (error.status === 503) {
      // Server overloaded, show loading spinner and retry
      console.warn('API busy, retrying...');
      throw error;
    } else {
      // Generic error - report to Sentry
      console.error('Unexpected scan error:', error);
      throw error;
    }
  }
}
```

---

## Performance Tips

1. **Compress images** before upload (max 1024px, JPEG quality 0.85)
2. **Reuse camera instances** to avoid cold starts
3. **Cache reference images** locally when possible
4. **Batch multiple requests** if scanning multiple buildings
5. **Use retry logic** with exponential backoff for network failures
6. **Monitor performance metrics** - track upload_ms, clip_comparison_ms, etc

---

## Example Complete App Flow

```typescript
async function completeScanFlow() {
  try {
    // 1. Show loading UI
    setLoading(true);

    // 2. Capture photo and GPS
    const photoFile = await capturePhoto();
    const gpsData = await getGpsData();

    // 3. Scan building
    const scanResult = await scanWithErrorHandling(photoFile, gpsData);

    // 4. Show results
    if (scanResult.show_picker) {
      // Show UI to select correct building
      const confirmedBin = await showBuildingPicker(scanResult.matches);

      // 5. Confirm building
      await confirmBuilding(scanResult.scan_id, confirmedBin);
    } else {
      // High confidence match, show directly
      showBuildingDetails(scanResult.matches[0]);
    }

    // 6. Optional: Collect feedback
    const rating = await getFeedbackFromUser();
    await submitFeedback(
      scanResult.scan_id,
      rating,
      'correct'
    );

  } catch (error) {
    // Show error message to user
    showError(error.message);
    reportErrorToSentry(error);
  } finally {
    setLoading(false);
  }
}
```

---

## TypeScript Types

```typescript
interface ScanResponse {
  scan_id: string;
  matches: Match[];
  show_picker: boolean;
  processing_time_ms: number;
  performance: {
    upload_ms: number;
    geospatial_ms: number;
    reference_images_ms: number;
    clip_comparison_ms: number;
  };
  debug_info?: {
    num_candidates: number;
    num_reference_images: number;
    num_matches: number;
  };
}

interface Match {
  bin: string;
  address: string;
  confidence: number;
  distance_meters: number;
  images_count?: number;
}

interface GpsData {
  latitude: number;
  longitude: number;
  bearing: number;
  pitch: number;
  accuracy?: number;
}

interface ScanDetails {
  scan_id: string;
  user_id?: string;
  matches: Match[];
  confirmed_bin?: string;
  confirmed_at?: string;
  feedback?: {
    rating: number;
    feedback_text?: string;
    feedback_type?: string;
  };
}
```

---

## Next Steps

1. Copy TypeScript types into your mobile app
2. Implement `scanBuilding()` function
3. Integrate camera capture with GPS/compass data
4. Test locally with `http://localhost:8000`
5. Deploy to Modal and update API_BASE_URL
6. Add Sentry for error tracking
7. Set up PostHog analytics
