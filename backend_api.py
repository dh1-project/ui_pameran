# backend_api.py
import json
from datetime import datetime
import mysql.connector

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# === config db ambil dari db_config.json milikmu ===
def load_db_config():
    with open("db_config.json", "r", encoding="utf-8") as f:
        return json.load(f)

DB = load_db_config()

def get_conn():
    return mysql.connector.connect(
        host=DB["host"],
        port=int(DB.get("port", 3306)),
        user=DB["user"],
        password=DB["password"],
        database=DB["database"],
        autocommit=True,
        connect_timeout=10,
    )

app = FastAPI()

# biar React/HTML bisa akses (hindari CORS error)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # nanti kalau sudah fix, ganti ke origin React kamu
    allow_methods=["*"],
    allow_headers=["*"],
)

class VitalsIn(BaseModel):
    emr_no: str
    room_id: str | None = None
    hr: int = 0
    rr: int = 0
    systolic: int | None = None
    diastolic: int | None = None
    fall_detected: int = 1  # 1 jatuh, 0 tidak

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/test-alarm")
def test_alarm(payload: VitalsIn):
    """
    Endpoint untuk tombol 'Test Alarm' dari UI.
    Insert langsung ke tabel vitals (bukan fall_events).
    """
    try:
        conn = get_conn()
        cur = conn.cursor()

        # ⚠️ WAJIB samakan dengan kolom vitals kamu.
        # Minimal: waktu, emr_no, rr, hr, fall_detected (contoh)
        sql = """
        INSERT INTO vitals (waktu, emr_no, hr, rr, systolic, diastolic, fall_detected)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        cur.execute(sql, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            payload.emr_no,
            int(payload.hr),
            int(payload.rr),
            payload.systolic,
            payload.diastolic,
            int(payload.fall_detected),
        ))

        cur.close()
        conn.close()
        return {"status": "inserted", "emr_no": payload.emr_no, "fall_detected": payload.fall_detected}

    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"MySQL error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
