from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
import requests
import certifi
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt
import csv
import io
import os
from fpdf import FPDF
import pytz
import time

# =====================================================
# 🚀 FASTAPI INIT
# =====================================================
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# THE PERFECT CORS POLICY
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://farm2future-frontend.vercel.app",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# =====================================================
# 🔐 AUTH CONFIG
# =====================================================
SECRET_KEY = "SMARTCROP_SECRET_KEY_CHANGE_THIS"
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

MONGO_URI = "mongodb+srv://smartcrop_user:SmartCrop123@cluster0.akjx7xj.mongodb.net/smart_crop_db?retryWrites=true&w=majority"

try:
    client = MongoClient(MONGO_URI, tls=True, tlsCAFile=certifi.where())
    db = client["smart_crop_db"]
    collection = db["sensor_data"]
    farmers_collection = db["farmers"]
    users_collection = db["users"]
    print("✅ MongoDB Connected")
except Exception as e:
    print(f"❌ MongoDB Connection Failed: {e}")

# =====================================================
# 🔐 AUTH HELPERS
# =====================================================
class LoginRequest(BaseModel):
    username: str
    password: str

# ✅ UPDATED: Added name and device_id for Admin hardware provisioning
class RegisterRequest(BaseModel):
    name: str = None
    username: str
    password: str
    device_id: str = None
    role: str = "farmer"

def create_token(data: dict):
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ✅ SPECIAL TOKEN CHECK FOR DOWNLOADS (URL PARAM)
def get_token_from_query(token: str = Query(...)):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# =====================================================
# 👤 USER ENDPOINTS (UPDATED FOR PROVISIONING)
# =====================================================
@app.post("/api/register")
def register_user(user: RegisterRequest):
    # 1. Check if username is taken
    if users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="Username already exists")
        
    # 2. Check if device_id is already assigned
    if user.device_id and farmers_collection.find_one({"device_id": user.device_id}):
        raise HTTPException(status_code=400, detail="Device ID is already assigned to another farmer")

    # 3. Create Secure Login Credentials
    users_collection.insert_one({
        "username": user.username,
        "password": pwd_context.hash(user.password),
        "role": user.role,
        "created_at": datetime.now(timezone.utc)
    })
    
    # 4. Provision Hardware & Link to Admin Dashboard
    if user.role == "farmer" and user.device_id:
        farmers_collection.insert_one({
            "name": user.name or user.username,
            "username": user.username,
            "device_id": user.device_id,
            "approved": True, # Automatically approved since Admin created it
            "created_at": datetime.now(timezone.utc)
        })

    return {"status": "User created and Hardware Provisioned", "role": user.role, "device_id": user.device_id}

@app.post("/api/login")
def login(data: LoginRequest):
    user = users_collection.find_one({"username": data.username})
    if not user or not pwd_context.verify(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token({"username": user["username"], "role": user["role"]}), "role": user["role"]}

@app.get("/api/admin/farmers")
def get_farmers(user=Depends(verify_token)):
    if user["role"] != "admin": raise HTTPException(status_code=403)
    return list(farmers_collection.find({"approved": True}, {"_id": 0, "name": 1, "device_id": 1, "username": 1}))

# =====================================================
# 📊 SENSOR DATA ENDPOINTS
# =====================================================
# Persistent store for controls
sensor_store = {"relay": 0, "mode": "AUTO"}

@app.get("/api/sensor-data")
def get_sensor_data(device_id: str, range: str = Query("24H"), user=Depends(verify_token)):
    # Calculate time cutoff
    hours = {"1H": 1, "24H": 24, "7D": 168, "30D": 720}.get(range.upper(), 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Fetch Latest Doc
    last_doc = collection.find_one({"device_id": device_id}, {"_id": 0}, sort=[("timestamp", -1)])
    
    if not last_doc:
        return {
            "online": False, 
            "soilMoisture": 0, "temperature": 0, "humidity": 0, "airQuality": 0, 
            "history": []
        }

    # Check online status (120s timeout)
    last_ts = last_doc.get("timestamp")
    online = False
    if last_ts:
        online = (datetime.now(timezone.utc) - last_ts.replace(tzinfo=timezone.utc)).total_seconds() < 120

    # Fetch History
    history_cursor = collection.find(
        {"device_id": device_id, "timestamp": {"$gte": cutoff}}, 
        {"_id": 0}
    ).sort("timestamp", 1)
    
    history = []
    for h in history_cursor:
        history.append({
            "timestamp": h["timestamp"],
            "soil": h.get("soil", 0),
            "temperature": h.get("temperature", 0),
            "humidity": h.get("humidity", 0)
        })

    return {
        "online": online,
        "soilMoisture": last_doc.get("soil", 0),
        "temperature": last_doc.get("temperature", 0),
        "humidity": last_doc.get("humidity", 0),
        "airQuality": last_doc.get("air_quality", 0),
        "relay": last_doc.get("relay", 0),
        "pir": last_doc.get("pir", 0),
        "mode": sensor_store["mode"],
        "cropHealth": "Good" if last_doc.get("soil", 0) > 30 else "Critical",
        "recommendation": "Water Now" if last_doc.get("soil", 0) < 40 else "Wait",
        "history": history
    }

# =====================================================
# 📈 ANALYTICS ENDPOINT (FIXED)
# =====================================================
@app.get("/api/analytics")
def get_analytics(device_id: str, user=Depends(verify_token)):
    # 1. Allow Admin and Farmer to view analytics
    if user["role"] not in ["admin", "farmer"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # 2. Look back 24 hours
    last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    
    # 🔍 DEBUG: Print the query parameters
    print(f"📊 ANALYTICS QUERY: Device={device_id} | Since={last_24h}")

    # 3. SIMPLIFIED QUERY (Removed strict 'soil' check)
    # We just want any data logged by this device in the last 24h
    docs = list(collection.find({
        "device_id": device_id,
        "timestamp": {"$gte": last_24h}
    }, {"_id": 0}))

    count = len(docs)
    print(f"✅ ANALYTICS: Found {count} records.")

    if not docs:
        return {
            "totalReadings": 0, 
            "avgSoil": 0, 
            "avgTemp": 0, 
            "avgHumidity": 0, 
            "wateringEvents": 0
        }

    # 4. ROBUST CALCULATION (Handles missing keys gracefully)
    # If 'soil' is missing, it defaults to 0, preventing errors
    avg_soil = sum(d.get("soil", 0) for d in docs) / count if count else 0
    avg_temp = sum(d.get("temperature", 0) for d in docs) / count if count else 0
    avg_hum = sum(d.get("humidity", 0) for d in docs) / count if count else 0
    watering = sum(1 for d in docs if d.get("relay") == 1)

    return {
        "totalReadings": count,
        "avgSoil": round(avg_soil, 1),
        "avgTemp": round(avg_temp, 1),
        "avgHumidity": round(avg_hum, 1),
        "wateringEvents": watering
    }

# =====================================================
# 🌱 ARDUINO UPDATE ENDPOINT
# =====================================================
@app.post("/api/update-data")
async def update_data(request: Request):
    data = await request.json()
    print("🔥 RAW FROM ARDUINO:", data)

    device_id = data.get("device_id")
    if not device_id: return {"error": "No ID"}

    # ROBUST KEY CHECKING
    soil_val = data.get("soilMoisture", data.get("soil", 0))
    air_val = data.get("airQuality", data.get("air_quality", 0))

    doc = {
        "device_id": device_id,
        "soil": int(soil_val),
        "temperature": float(data.get("temperature", 0)),
        "humidity": float(data.get("humidity", 0)),
        "air_quality": int(air_val),
        "pir": int(data.get("pir", 0)),
        "relay": int(data.get("relay", 0)),
        "timestamp": datetime.now(timezone.utc)
    }

    collection.insert_one(doc)
    
    # Update global relay state for Arduino to read back
    # Only update relay from sensor logic if in AUTO mode
    if sensor_store["mode"] == "AUTO":
        if doc["soil"] < 35: sensor_store["relay"] = 1
        elif doc["soil"] > 50: sensor_store["relay"] = 0
    
    # Store state for manual override persistence
    sensor_store["relay"] = doc["relay"] if sensor_store["mode"] == "MANUAL" else sensor_store["relay"]

    return {"status": "OK"}

@app.get("/api/esp32-command")
def esp32_command():
    return sensor_store


# =====================================================
# 📄 DOWNLOAD ENDPOINTS
# =====================================================

@app.get("/api/export/csv")
def export_csv(device_id: str, user=Depends(get_token_from_query)):
    print(f"📊 GENERATING CSV FOR: {device_id}") # Debug Log
    
    try:
        # Fetch Data
        docs = list(collection.find({"device_id": device_id}, {"_id": 0}).sort("timestamp", -1).limit(100))
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Time", "Soil", "Temp", "Humidity", "Air Quality"])
        
        ist = pytz.timezone('Asia/Kolkata')
        
        for d in docs:
            raw_ts = d.get('timestamp')
            
            # SAFE DATE HANDLING: Check if it's actually a datetime object before converting
            if raw_ts and hasattr(raw_ts, 'replace'):
                try:
                    ts = raw_ts.replace(tzinfo=pytz.utc).astimezone(ist).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts = str(raw_ts) # Fallback if timezone math fails
            else:
                ts = str(raw_ts) if raw_ts else "N/A"
                
            # Write row with safe defaults
            writer.writerow([
                ts, 
                d.get('soil', 0), 
                d.get('temperature', 0), 
                d.get('humidity', 0), 
                d.get('air_quality', 0)
            ])
        
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode('utf-8')), 
            media_type="text/csv", 
            headers={"Content-Disposition": f"attachment; filename={device_id}.csv"}
        )

    except Exception as e:
        # 🛑 THIS WILL PRINT THE EXACT ERROR TO YOUR TERMINAL
        print(f"❌ CSV CRASHED: {str(e)}")
        # Return a text file with the error so you can see it
        return StreamingResponse(
            io.BytesIO(f"Error generating CSV: {str(e)}".encode('utf-8')),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=csv_error_log.txt"}
        )


@app.get("/api/export/pdf")
def export_pdf(device_id: str, user=Depends(get_token_from_query)):
    print(f"📄 GENERATING PDF FOR: {device_id}") # Debug Log
    
    try:
        # 1. Fetch Data
        docs = list(collection.find({"device_id": device_id}, {"_id": 0}).sort("timestamp", -1).limit(50))
        
        # 2. Initialize PDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=10)
        
        # Header
        pdf.cell(0, 10, txt=f"Report: {device_id}", ln=1, align='C')
        
        # 3. Process Data Safely
        ist = pytz.timezone('Asia/Kolkata')
        
        for d in docs:
            raw_ts = d.get('timestamp')
            
            # SAFE DATE HANDLING: Check if it's actually a datetime object
            if raw_ts and hasattr(raw_ts, 'replace'):
                ts = raw_ts.replace(tzinfo=pytz.utc).astimezone(ist).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ts = str(raw_ts) if raw_ts else "N/A"
                
            line = f"{ts} | Soil: {d.get('soil',0)}% | T: {d.get('temperature',0)}C | Hum: {d.get('humidity',0)}%"
            
            # Add line to PDF
            pdf.cell(0, 8, txt=line, ln=1)

        # 4. Generate Output (Handle both FPDF v1 and v2)
        try:
            # Try new FPDF2 syntax first (returns bytes)
            pdf_bytes = pdf.output()
            if isinstance(pdf_bytes, str): 
                pdf_bytes = pdf_bytes.encode('latin-1') # Fallback for v1 returning string
        except TypeError:
            # Fallback for old FPDF v1 (requires dest='S')
            pdf_bytes = pdf.output(dest='S').encode('latin-1')

        return StreamingResponse(
            io.BytesIO(pdf_bytes), 
            media_type="application/pdf", 
            headers={"Content-Disposition": f"attachment; filename={device_id}.pdf"}
        )

    except Exception as e:
        # 🛑 THIS WILL PRINT THE EXACT ERROR TO YOUR TERMINAL
        print(f"❌ PDF CRASHED: {str(e)}")
        # Return a text file with the error so you can see it in the browser
        return StreamingResponse(
            io.BytesIO(f"Error generating PDF: {str(e)}".encode()),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=error_log.txt"}
        )

# =====================================================
# ⚙️ MODE & RELAY CONTROL
# =====================================================
@app.post("/api/manual-control")
async def manual_control(request: Request, user=Depends(verify_token)):
    if user["role"] not in ["admin", "farmer"]:
        raise HTTPException(status_code=403, detail="Access denied")
        
    data = await request.json()
    action = data.get("action")
    if action == "ON": 
        sensor_store["mode"] = "MANUAL"
        sensor_store["relay"] = 1
    elif action == "OFF":
        sensor_store["mode"] = "MANUAL"
        sensor_store["relay"] = 0
        
    return {"status": "OK", "relay": sensor_store["relay"]}

@app.post("/api/set-mode")
async def set_mode(request: Request, user=Depends(verify_token)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    
    data = await request.json()
    sensor_store["mode"] = data.get("mode", "AUTO")
    return {"status": "OK", "mode": sensor_store["mode"]}

@app.get("/api/pir-logs")
def get_pir_logs(device_id: str, user=Depends(verify_token)):
    if user["role"] != "admin": raise HTTPException(status_code=403)
    # Returns last 10 motion events (Exclude _id)
    logs = list(collection.find(
        {"device_id": device_id, "pir": 1}, 
        {"_id": 0}
    ).sort("timestamp", -1).limit(10))

    return logs

