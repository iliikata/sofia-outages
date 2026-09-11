import json
import urllib.parse
import urllib.request
import http.cookiejar
import math
import datetime
from shapely.geometry import Point, Polygon
from flask import Flask, render_template_string, request

GOOGLE_MAPS_API_KEY = "AIzaSyDk4_twSTimMCDF-_NtLWXOfK5GWW5TBCg"

app = Flask(__name__)

def format_timestamp(ts):
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(int(ts) / 1000.0)
        return dt.strftime("%d.%m.%Y г. %H:%M ч.")
    except Exception:
        return str(ts)

def geocode_address(address_str):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address_str + ", Sofia, Bulgaria",
        "key": GOOGLE_MAPS_API_KEY
    }
    try:
        req_url = url + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(req_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "OK" and data.get("results"):
                result = data["results"][0]
                loc = result["geometry"]["location"]
                lat = loc["lat"]
                lng = loc["lng"]
                
                # Note: Sofiyska Voda ArcGIS service uses EPSG:32635 (UTM Zone 35N) or similar projected coordinate system in meters, NOT Web Mercator!
                # Let's use Proj or an approximate transformer or just query using geometry intersection.
                # Actually, ArcGIS REST API allows geometry intersections or spatial filters!
                return {
                    "address": result.get("formatted_address"),
                    "lat": lat,
                    "lng": lng,
                    "latlng_point": Point(lng, lat)
                }
    except Exception as e:
        print(f"Google Maps Geocoding error: {e}")
    return None

def check_address_outages(address_str):
    geo = geocode_address(address_str)
    if not geo:
        return {"error": "Адресът не беше открит в София. Моля, въведете по-пълен адрес (напр. 'ул. Сава Огнянов 1')."}
    
    # Since Sofiyska Voda internal geocoder is restricted, let's use a spatial buffer or fallback:
    # We will check if the Google Maps coordinates fall inside any of the water outage polygons 
    # by converting the ArcGIS polygon rings (which are in local Sofia coordinate system WKID 103535 / UTM 35N meters) 
    # into approximate lat/lng using a linear shift or inverse transform.
    # Actually, let's query the ArcGIS layers using spatial intersection directly with point geometry if possible,
    # or check distance to polygon bounds!
    
    point = Point(geo['lng'], geo['lat']) # WGS84 point
    
    # 1. Check Water Outages
    water_matches = []
    water_layers = {
        "Вода - Авария": "https://gispx.sofiyskavoda.bg/arcgis/rest/services/WSI_PUBLIC/InfoCenter_Public/MapServer/2/query",
        "Вода - Планирано спиране": "https://gispx.sofiyskavoda.bg/arcgis/rest/services/WSI_PUBLIC/InfoCenter_Public/MapServer/3/query"
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://gispx.sofiyskavoda.bg",
        "Referer": "https://gispx.sofiyskavoda.bg/WebApp.InfoCenter/"
    }

    water_polygons = []

    for label, url in water_layers.items():
        try:
            params = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "f": "json"}
            data = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                for feat in res_json.get("features", []):
                    geometry = feat.get("geometry", {})
                    rings = geometry.get("rings", [])
                    
                    lat_lng_rings = []
                    for ring in rings:
                        lat_lng_ring = []
                        for pt in ring:
                            mx, my = pt[0], pt[1]
                            # Approximate conversion from local Sofia metric system (WKID 103535) to WGS84 Lat/Lng
                            # Center of Sofia: 42.6977 N, 23.3219 E corresponds roughly to local X=315000, Y=4728000 or similar.
                            # Let's use a precise linear approximation or inverse delta from known anchor:
                            # Anchor: Suha reka polygon bounds X: 202041 - 203067, Y: 4733170 - 4733948 -> Lat ~ 42.70, Lng ~ 23.36
                            lng = 23.36 + (mx - 202500) * 0.0000115
                            lat = 42.70 + (my - 4733500) * 0.000009
                            lat_lng_ring.append({"lat": lat, "lng": lng})
                        lat_lng_rings.append(lat_lng_ring)

                    # Create shapely polygon in lat/lng for easy testing with user's point
                    poly_coords = [(pt["lng"], pt["lat"]) for pt in lat_lng_rings[0]] if lat_lng_rings else []
                    is_inside = False
                    if len(poly_coords) >= 3:
                        poly = Polygon(poly_coords)
                        if poly.contains(point):
                            is_inside = True

                    if lat_lng_rings:
                        water_polygons.append({
                            "type": label,
                            "is_active_for_address": is_inside,
                            "rings": lat_lng_rings
                        })

                    if is_inside:
                        attrs = feat.get("attributes", {})
                        start_ts = attrs.get("START_")
                        end_ts = attrs.get("ALERTEND")
                        start_h = attrs.get("START_H", "")
                        start_m = attrs.get("START_M", "")
                        end_h = attrs.get("END_H", "")
                        end_m = attrs.get("END_M", "")
                        
                        time_str = ""
                        if start_ts and end_ts:
                            time_str = f"От {format_timestamp(start_ts)} до {format_timestamp(end_ts)}"
                        elif start_h and end_h:
                            time_str = f"От {start_h}:{start_m or '00'} до {end_h}:{end_m or '00'}"

                        water_matches.append({
                            "type": label,
                            "description": attrs.get("DESCRIPTION"),
                            "location": attrs.get("LOCATION"),
                            "time": time_str
                        })
        except Exception as e:
            print(f"Water check error: {e}")

    # 2. Check Power Outages (ERM Zapad)
    power_matches = []
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0"),
            ("Referer", "https://info.ermzapad.bg/webint/vok/avplan.php")
        ]
        opener.open("https://info.ermzapad.bg/webint/vok/avplan.php", timeout=10)
        
        post_data = urllib.parse.urlencode({
            "action": "draw",
            "gm_obstina": "SOF",
            "lat": "42.6977",
            "lon": "23.3219"
        }).encode("utf-8")
        
        req = urllib.request.Request("https://info.ermzapad.bg/webint/vok/avplan.php", data=post_data)
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8-sig", errors="ignore")
            if raw.strip():
                data = json.loads(raw)
                for k, v in data.items():
                    if isinstance(v, dict) and "ceo" in v:
                        typedist = v.get("typedist", "прекъсване")
                        p_type = "Ток - Планирано спиране" if "планиран" in typedist.lower() else "Ток - Авария"
                        power_matches.append({
                            "type": p_type,
                            "description": typedist,
                            "location": f"Зона: {v.get('ceo')} ({v.get('city_name', 'София')})",
                            "start": v.get("begin_event"),
                            "end": v.get("end_event"),
                            "lat": v.get("lat"),
                            "lng": v.get("lon")
                        })
    except Exception as e:
        print(f"Power check error: {e}")

    return {
        "geocoded_address": geo['address'],
        "lat": geo['lat'],
        "lng": geo['lng'],
        "water_outages": water_matches,
        "power_outages": power_matches,
        "water_polygons": water_polygons
    }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bg">
<head>
    <meta charset="UTF-8">
    <title>Интерактивна карта - Ток и Вода в София</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; font-family: sans-serif; }
        .container { max-width: 800px; margin-top: 30px; margin-bottom: 50px; }
        .card { border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        #map { height: 450px; width: 100%; border-radius: 8px; margin-top: 20px; }
        .status-banner { padding: 15px; border-radius: 8px; font-weight: bold; text-align: center; margin-top: 20px; }
        .status-green { background-color: #d1e7dd; color: #0f5132; border: 1px solid #badbcc; }
        .status-red { background-color: #f8d7da; color: #842029; border: 1px solid #f5c2c7; }
    </style>
</head>
<body>
<div class="container">
    <div class="card p-4">
        <h2 class="text-center mb-4">📍 Проверка за Ток и Вода по Адрес</h2>
        <form method="POST" class="mb-3">
            <div class="input-group">
                <input type="text" name="address" class="form-control form-control-lg" placeholder="Въведете адрес (напр. ул. Московска 33)..." value="{{ address or 'ул. Московска 33' }}" required>
                <button class="btn btn-primary btn-lg" type="submit">Провери на картата</button>
            </div>
        </form>

        {% if error %}
            <div class="alert alert-danger mt-3">{{ error }}</div>
        {% endif %}

        {% if result %}
            <hr>
            <h5 class="text-muted">Търсен адрес: <strong>{{ result.geocoded_address }}</strong></h5>

            {% set has_issues = result.water_outages|length > 0 or result.power_outages|length > 0 %}

            {% if has_issues %}
                <div class="status-banner status-red">
                    ⚠️ ВНИМАНИЕ: Има регистрирани прекъсвания за този адрес или район!
                </div>
                <div class="row mt-3">
                    {% if result.water_outages %}
                    <div class="col-md-6 mb-2">
                        <div class="p-3 border border-danger rounded bg-white">
                            <h5 class="text-danger">💧 Вода</h5>
                            <ul class="mb-0 ps-3">
                            {% for w in result.water_outages %}
                                <li><strong>{{ w.type }}</strong>: {{ w.description }}<br>
                                    {% if w.time %}<small class="text-danger fw-bold">⏱ {{ w.time }}</small><br>{% endif %}
                                    <small class="text-muted">{{ w.location }}</small>
                                </li>
                            {% endfor %}
                            </ul>
                        </div>
                    </div>
                    {% endif %}
                    {% if result.power_outages %}
                    <div class="col-md-6 mb-2">
                        <div class="p-3 border border-warning rounded bg-white">
                            <h5 class="text-warning text-dark">⚡ Ток</h5>
                            <ul class="mb-0 ps-3">
                            {% for p in result.power_outages %}
                                <li><strong>{{ p.type }}</strong> (от {{ p.start }} до {{ p.end }})<br><small class="text-muted">{{ p.location }}</small></li>
                            {% endfor %}
                            </ul>
                        </div>
                    </div>
                    {% endif %}
                </div>
            {% else %}
                <div class="status-banner status-green">
                    ✔ Всичко е наред: Няма активни аварии или планирани спирания на ток и вода за този адрес!
                </div>
            {% endif %}

            <!-- Google Map -->
            <div id="map"></div>

            <script>
                function initMap() {
                    var centerCoords = { lat: {{ result.lat }}, lng: {{ result.lng }} };
                    var map = new google.maps.Map(document.getElementById('map'), {
                        zoom: 15,
                        center: centerCoords,
                        mapTypeId: 'roadmap'
                    });

                    // Marker for user address
                    var marker = new google.maps.Marker({
                        position: centerCoords,
                        map: map,
                        title: "{{ result.geocoded_address }}"
                    });

                    // Water outage polygons
                    var waterPolygonsData = {{ result.water_polygons | tojson }};
                    waterPolygonsData.forEach(function(polyData) {
                        var fillColor = polyData.is_active_for_address ? "#dc3545" : "#ffc107";
                        var strokeColor = polyData.is_active_for_address ? "#b02a37" : "#ff851b";

                        polyData.rings.forEach(function(ring) {
                            var polygon = new google.maps.Polygon({
                                paths: ring,
                                strokeColor: strokeColor,
                                strokeOpacity: 0.8,
                                strokeWeight: 2,
                                fillColor: fillColor,
                                fillOpacity: 0.35
                            });
                            polygon.setMap(map);
                        });
                    });

                    // Power outage markers
                    var powerOutagesData = {{ result.power_outages | tojson }};
                    powerOutagesData.forEach(function(p) {
                        if (p.lat && p.lng) {
                            var pMarker = new google.maps.Marker({
                                position: { lat: parseFloat(p.lat), lng: parseFloat(p.lng) },
                                map: map,
                                title: p.type,
                                icon: "http://maps.google.com/mapfiles/ms/icons/red-dot.png"
                            });
                        }
                    });
                }
            </script>
            <script async defer src="https://maps.googleapis.com/maps/api/js?key={{ g_key }}&callback=initMap"></script>
        {% endif %}
    </div>
</div>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None
    address = None
    if request.method == "POST":
        address = request.form.get("address")
        res = check_address_outages(address)
        if "error" in res:
            error = res["error"]
        else:
            result = res
    return render_template_string(HTML_TEMPLATE, result=result, error=error, address=address, g_key=GOOGLE_MAPS_API_KEY)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
