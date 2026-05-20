#!/bin/bash

# Test all 6 buildings

echo "=== 1. Seagram Building (375 Park Ave) ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building.jpg \
  -F gps_lat=40.7583786443 \
  -F gps_lng=-73.9723321538 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== 2. Radiator Building (40 West 40th St) ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building2.jpeg \
  -F gps_lat=40.7527775971 \
  -F gps_lng=-73.9841953778 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== 3. 140 Broadway ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building3.jpg \
  -F gps_lat=40.7086127316 \
  -F gps_lng=-74.0102946622 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== 4. Woolworth Building (233 Broadway) ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building4.jpg \
  -F gps_lat=40.7124429197 \
  -F gps_lng=-74.0086358395 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== 5. Lever House (390 Park Ave) ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building5.jpg \
  -F gps_lat=40.7596067630 \
  -F gps_lng=-73.9735972074 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== 6. CBS Building (51 West 52nd St) ==="
curl -s -X POST http://localhost:8000/api/scan \
  -F photo=@test_building6.jpg \
  -F gps_lat=40.7612474690 \
  -F gps_lng=-73.9788100133 \
  -F compass_bearing=90 \
  -F phone_pitch=45 | python3 -m json.tool | grep -E '"(address|confidence|processing_time)'

echo -e "\n=== All tests complete ==="
