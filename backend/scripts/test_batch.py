import requests

tests = [
    {
        'name': '140 Broadway',
        'file': 'test_building3.jpg',
        'bbl': '1000480001',
        'lat': 40.7086127319474,
        'lng': -74.0100196964792
    },
    {
        'name': 'Woolworth Building',
        'file': 'test_building4.jpg',
        'bbl': '1001237501',
        'lat': 40.7124429197594,
        'lng': -74.0083137399404
    },
    {
        'name': 'Lever House',
        'file': 'test_building5.jpg',
        'bbl': '1012890036',
        'lat': 40.759606762823,
        'lng': -73.9728719285694
    },
    {
        'name': 'CBS Building',
        'file': 'test_building6.jpg',
        'bbl': '1012680001',
        'lat': 40.7612474690527,
        'lng': -73.9787823995077
    }
]

print('Testing 4 buildings with CORRECTED BBLs...')
print()

results = []
for test in tests:
    print(f"Testing: {test['name']}")
    
    with open(test['file'], 'rb') as f:
        response = requests.post('http://localhost:8000/api/phase1/scan',
            files={'photo': f},
            data={
                'lat': test['lat'],
                'lng': test['lng'],
                'gps_accuracy': 10,
                'bearing': 0,
                'pitch': 20
            }
        )
    
    result = response.json()
    
    if 'error' in result:
        print(f"  ❌ ERROR: {result['error']}")
        results.append({
            'building': test['name'],
            'correct': False,
            'confidence': 0,
            'latency': result.get('latency_ms', 0)
        })
    else:
        correct = result['building']['bbl'] == test['bbl']
        confidence = result['confidence']
        latency = result['latency_ms']
        
        status = '✅' if correct else '❌'
        print(f"  {status} Match: {result['building']['name']}")
        print(f"     Confidence: {confidence:.1%}")
        print(f"     Latency: {latency:.0f}ms")
        
        results.append({
            'building': test['name'],
            'correct': correct,
            'matched': result['building']['name'],
            'confidence': confidence,
            'latency': latency
        })
    
    print()

# Summary
print('='*50)
print('FINAL RESULTS - CORRECT BBLs')
print('='*50)
correct_count = sum(1 for r in results if r['correct'])
total = len(results)
precision = correct_count / total if total > 0 else 0
avg_confidence = sum(r['confidence'] for r in results) / total
avg_latency = sum(r['latency'] for r in results) / total

print(f"Precision@1: {precision:.1%} ({correct_count}/{total})")
print(f"Avg Confidence: {avg_confidence:.1%}")
print(f"Avg Latency: {avg_latency:.0f}ms")
print()

print('Baseline (old dataset): 75.0% precision, 77.0% confidence')
if precision >= 0.75:
    print('✅ PHASE 1 COMPLETE - Ready for Phase 2')
else:
    print('Review failures above')
