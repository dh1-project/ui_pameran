import sys
import cv2
import json
import os
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QCheckBox,
)
from PySide6.QtCore import QThread, Signal, Qt, QMutex, QWaitCondition
from PySide6.QtGui import QImage, QPixmap, QFont

import mysql.connector

# MQTT optional
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


MODERN_STYLE = """
QMainWindow { background: #1a1a2e; }
QWidget { color: #eaeaea; font-family: 'Segoe UI'; }
QDialog QLabel { color: #333333; font-weight: 600; }
QGroupBox { border: 2px solid #0f3460; border-radius: 8px; padding: 12px; background: rgba(15,52,96,0.3); }
QPushButton { background: #0f3460; border: 2px solid #00d4ff; border-radius: 6px; padding: 8px; color: white; }
QPushButton#startBtn { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #00d4ff,stop:1 #0099cc); }
QPushButton#stopBtn { background: #ff4757; }
QLineEdit, QSpinBox { background: rgba(26,26,46,0.8); border: 2px solid #0f3460; border-radius: 4px; padding: 6px; color: #eaeaea; }
QLabel#titleLabel { font-size: 22px; color: #00d4ff; font-weight: bold; }
QLabel#statusLabel { font-weight: bold; }
#videoFrame { background: black; border: 2px solid #00d4ff; border-radius: 8px; }
"""


def fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds//60:02}:{seconds%60:02}"


def payload_is_fall(payload: dict) -> bool:
    """
    Heuristik deteksi 'jatuh' dari payload mmWave.
    Kamu bisa sesuaikan sesuai format MR60FDA2 di RSI.
    """
    # paling umum
    if payload.get("fall_detected") in (1, True, "1", "true", "TRUE"):
        return True

    # beberapa device pakai event/type/status
    for k in ("event", "type", "status", "alarm"):
        v = payload.get(k)
        if isinstance(v, str) and "fall" in v.lower():
            return True

    # contoh lain: classification = FALL
    v = payload.get("classification")
    if isinstance(v, str) and v.lower() == "fall":
        return True

    return False


class MmwaveMqttThread(QThread):
    status = Signal(str)
    message = Signal(dict)

    def __init__(self, host, port, topic, username="", password=""):
        super().__init__()
        self.host = host
        self.port = int(port)
        self.topic = topic
        self.username = username or ""
        self.password = password or ""
        self._stop = False
        self._client = None
        self.monitoring_armed = False

    def run(self):
        if mqtt is None:
            self.status.emit("MQTT lib missing (pip install paho-mqtt)")
            return

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                self.status.emit("🟢 mmWave MQTT Connected")
                client.subscribe(self.topic)
            else:
                self.status.emit(f"🔴 mmWave MQTT Connect Failed rc={rc}")

        def on_message(client, userdata, msg):
            try:
                raw = msg.payload.decode("utf-8", errors="ignore")
                data = json.loads(raw) if raw.strip().startswith("{") else {"raw": raw}
                data["_topic"] = msg.topic
                self.message.emit(data)
            except Exception as e:
                self.status.emit(f"mmWave parse error: {e}")

        self._client = mqtt.Client()
        if self.username:
            self._client.username_pw_set(self.username, self.password)

        self._client.on_connect = on_connect
        self._client.on_message = on_message

        try:
            self._client.connect(self.host, self.port, 60)
            self._client.loop_start()
            while not self._stop:
                self.msleep(100)
        except Exception as e:
            self.status.emit(f"mmWave MQTT error: {e}")
        finally:
            try:
                if self._client:
                    self._client.loop_stop()
                    self._client.disconnect()
            except Exception:
                pass

    def stop(self):
        self._stop = True


class VideoThread(QThread):
    change_pixmap = Signal(QImage)
    update_time = Signal(str)
    update_time_sec = Signal(float)
    fall_by_timer = Signal()  # fallback timer trigger
    finished = Signal()

    def __init__(self, video_path, fall_time_sec, start_frame=0, fall_already_triggered=False):
        super().__init__()
        self.video_path = video_path
        self.fall_time_sec = fall_time_sec

        self._stop = False
        self._paused = False
        self._mutex = QMutex()
        self._pause_cond = QWaitCondition()

        self.start_frame = start_frame
        self.current_frame = start_frame
        self.fall_triggered = fall_already_triggered

    def run(self):
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            self.finished.emit()
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1

        cap.set(cv2.CAP_PROP_POS_FRAMES, self.start_frame)
        self.current_frame = self.start_frame

        while cap.isOpened():
            # pause handling
            self._mutex.lock()
            while self._paused and not self._stop:
                self._pause_cond.wait(self._mutex)
            should_stop = self._stop
            self._mutex.unlock()

            if should_stop:
                break

            ret, frame = cap.read()
            if not ret:
                break

            current_time = self.current_frame / fps
            total_time = total_frames / fps
            cur = f"{int(current_time//60):02}:{int(current_time%60):02}"
            tot = f"{int(total_time//60):02}:{int(total_time%60):02}"
            self.update_time.emit(f"{cur} / {tot}")
            self.update_time_sec.emit(current_time)

            # fallback timer trigger
            if (not self.fall_triggered) and current_time >= self.fall_time_sec:
                self.fall_by_timer.emit()
                self.fall_triggered = True

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qt_img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
            self.change_pixmap.emit(qt_img)

            self.current_frame += 1
            self.msleep(max(1, int(1000 / fps)))

        cap.release()
        self.finished.emit()

    def stop(self):
        self._mutex.lock()
        self._stop = True
        self._paused = False
        self._pause_cond.wakeAll()
        self._mutex.unlock()

    def toggle_pause(self):
        self._mutex.lock()
        self._paused = not self._paused
        if not self._paused:
            self._pause_cond.wakeAll()
        self._mutex.unlock()

    def is_paused(self):
        self._mutex.lock()
        p = self._paused
        self._mutex.unlock()
        return p

    def mark_fall_triggered(self):
        # dipanggil dari UI saat mmWave trigger
        self._mutex.lock()
        self.fall_triggered = True
        self._mutex.unlock()


class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🚨 Fall Detection System")
        self.resize(1000, 850)
        self.setStyleSheet(MODERN_STYLE)

        self.video_path = None
        self.video_thread = None
        self.mmwave_thread = None

        self.db_config = self.load_db_config()
        self.app_config = self.load_app_config()
        self.db_conn = None

        self.last_frame = 0
        self.fall_triggered = False
        self.current_time_str = "00:00"
        self.current_time_sec = 0.0

        self.init_ui()
        self.connect_db()
        self.start_mmwave_if_enabled()

    # ─── LOAD & SAVE: DB CONFIG ─────────────────────────────────────
    def load_db_config(self):
        config_file = "db_config.json"
        default = {
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "fall_detection_db",
        }
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in default.items():
                    data.setdefault(k, v)
                return data
            except Exception:
                return default

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
        return default

    def save_db_config(self, host, port, user, password, database):
        self.db_config = {
            "host": host,
            "port": int(port),
            "user": user,
            "password": password,
            "database": database,
        }
        with open("db_config.json", "w", encoding="utf-8") as f:
            json.dump(self.db_config, f, indent=4)

    # ─── LOAD & SAVE: APP CONFIG ────────────────────────────────────
    def load_app_config(self):
        config_file = "app_config.json"
        default = {
            "table_name": "fall_events",
            "room_id": "ROOM_01",
            "device_id": "CAM_001",
            "mmwave": {
                "enabled": False,
                "transport": "mqtt",
                "host": "127.0.0.1",
                "port": 1883,
                "topic": "rsi/mmwave/#",
                "username": "",
                "password": "",
            },
        }
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # merge defaults
                for k, v in default.items():
                    data.setdefault(k, v)
                if "mmwave" not in data or not isinstance(data["mmwave"], dict):
                    data["mmwave"] = default["mmwave"]
                else:
                    for k, v in default["mmwave"].items():
                        data["mmwave"].setdefault(k, v)
                return data
            except Exception:
                return default

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
        return default

    def save_app_config(self, table_name, room_id, device_id):
        # mmwave section keep as is
        mmw = self.app_config.get("mmwave", {})
        self.app_config = {"table_name": table_name, "room_id": room_id, "device_id": device_id, "mmwave": mmw}
        with open("app_config.json", "w", encoding="utf-8") as f:
            json.dump(self.app_config, f, indent=4)

    # ─── DATABASE ───────────────────────────────────────────────────
    def connect_db(self):
        try:
            # close old connection if any
            try:
                if self.db_conn and self.db_conn.is_connected():
                    self.db_conn.close()
            except Exception:
                pass

            self.db_conn = mysql.connector.connect(
                host=self.db_config["host"],
                port=self.db_config["port"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
                autocommit=True,
                connect_timeout=10,
            )
            if self.db_conn.is_connected():
                self.setup_table()
                self.db_status.setText("🟢 Connected")
                self.db_status.setStyleSheet("color: #00ff00;")
        except Exception:
            self.db_status.setText("🔴 Disconnected")
            self.db_status.setStyleSheet("color: #ff4757;")

    def setup_table(self):
        cursor = self.db_conn.cursor()
        table = self.app_config["table_name"]
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{table}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                data JSON NOT NULL
            ) ENGINE=InnoDB
            """
        )
        cursor.close()

    # ─── UI ─────────────────────────────────────────────────────────
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel("TEST ALARM FALL DETECTION SYSTEM")
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        db_row = QHBoxLayout()
        self.db_status = QLabel("🔴 Disconnected")
        self.db_status.setObjectName("statusLabel")
        db_row.addWidget(QPushButton("⚙️ DB Config", clicked=self.open_db_config_dialog))
        db_row.addWidget(QPushButton("🛠️ App Config", clicked=self.open_app_config_dialog))
        db_row.addWidget(self.db_status)
        db_row.addWidget(QPushButton("🔄 Reconnect", clicked=self.connect_db))
        layout.addLayout(db_row)

        # mmWave toggle row
        mmw_row = QHBoxLayout()
        self.mmwave_checkbox = QCheckBox("Use mmWave (LIVE)")
        self.mmwave_checkbox.setChecked(bool(self.app_config.get("mmwave", {}).get("enabled", False)))
        self.mmwave_checkbox.stateChanged.connect(self.on_mmwave_toggle)
        mmw_row.addWidget(self.mmwave_checkbox)
        self.mmwave_status = QLabel("mmWave: idle")
        self.mmwave_status.setStyleSheet("color:#aaa;")
        mmw_row.addWidget(self.mmwave_status)
        mmw_row.addStretch()
        layout.addLayout(mmw_row)

        vid_row = QHBoxLayout()
        vid_row.addWidget(QPushButton("📁 Select Video", clicked=self.select_video))
        self.vid_label = QLabel("No video selected")
        self.vid_label.setStyleSheet("color: #aaa;")
        vid_row.addWidget(self.vid_label)
        layout.addLayout(vid_row)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("⏰ Fall at (fallback):"))
        self.min_spin = QSpinBox(minimum=0, maximum=59, value=0, suffix=" min")
        self.sec_spin = QSpinBox(minimum=0, maximum=59, value=10, suffix=" sec")
        time_row.addWidget(self.min_spin)
        time_row.addWidget(QLabel(":"))
        time_row.addWidget(self.sec_spin)
        time_row.addStretch()
        layout.addLayout(time_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        self.start_btn = QPushButton("▶️ START", clicked=self.start_video)
        self.start_btn.setObjectName("startBtn")

        self.pause_btn = QPushButton("⏸️ PAUSE", clicked=self.pause_resume_video)
        self.pause_btn.setEnabled(False)

        self.stop_btn = QPushButton("⏹️ STOP", clicked=self.stop_video)
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)

        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setMinimumHeight(42)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        self.video_display = QLabel()
        self.video_display.setObjectName("videoFrame")
        self.video_display.setMinimumSize(400, 300)
        self.video_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_display.setAlignment(Qt.AlignCenter)
        self.video_display.mousePressEvent = self.on_video_clicked
        layout.addWidget(self.video_display)

        self.time_label = QLabel("00:00 / 00:00")
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.time_label)
        layout.addWidget(self.status_label)

        layout.addWidget(QPushButton("View History", clicked=self.show_history))

    # ─── mmWave toggle ───────────────────────────────────────────────
    def on_mmwave_toggle(self, state):
        enabled = (state == Qt.Checked)
        self.app_config["mmwave"]["enabled"] = enabled
        with open("app_config.json", "w", encoding="utf-8") as f:
            json.dump(self.app_config, f, indent=4)
        self.start_mmwave_if_enabled()

    def start_mmwave_if_enabled(self):
        # stop existing
        if self.mmwave_thread and self.mmwave_thread.isRunning():
            self.mmwave_thread.stop()
            self.mmwave_thread.wait(1000)
        self.mmwave_thread = None

        mmw = self.app_config.get("mmwave", {})
        if not mmw.get("enabled", False):
            self.mmwave_status.setText("mmWave: disabled")
            self.mmwave_status.setStyleSheet("color:#aaa;")
            return

        if mqtt is None:
            self.mmwave_status.setText("mmWave: paho-mqtt not installed")
            self.mmwave_status.setStyleSheet("color:#ff4757;")
            return

        self.mmwave_thread = MmwaveMqttThread(
            host=mmw.get("host", "127.0.0.1"),
            port=mmw.get("port", 1883),
            topic=mmw.get("topic", "rsi/mmwave/#"),
            username=mmw.get("username", ""),
            password=mmw.get("password", ""),
        )
        self.mmwave_thread.status.connect(self.on_mmwave_status)
        self.mmwave_thread.message.connect(self.on_mmwave_message)
        self.mmwave_thread.start()
        self.mmwave_status.setText("mmWave: connecting...")
        self.mmwave_status.setStyleSheet("color:#00d4ff;")

    def on_mmwave_status(self, s: str):
        self.mmwave_status.setText(s)
        if "🟢" in s:
            self.mmwave_status.setStyleSheet("color:#00ff00;")
        elif "🔴" in s or "error" in s.lower() or "missing" in s.lower():
            self.mmwave_status.setStyleSheet("color:#ff4757;")
        else:
            self.mmwave_status.setStyleSheet("color:#00d4ff;")

    def on_mmwave_message(self, payload: dict):
        # ✅ boleh trigger kalau monitoring_armed = True
        if not self.monitoring_armed:
            return

        if payload_is_fall(payload):
            self.trigger_fall(source="mmwave", raw_payload=payload)

    # ─── Click video to pause/resume ────────────────────────────────
    def on_video_clicked(self, event):
        if event.button() == Qt.LeftButton:
            self.pause_resume_video()

    # ─── Pause / Resume ─────────────────────────────────────────────
    def pause_resume_video(self):
        if not self.video_thread or not self.video_thread.isRunning():
            return

        self.video_thread.toggle_pause()
        if self.video_thread.is_paused():
            self.pause_btn.setText("▶️ RESUME")
            self.status_label.setText("⏸️ Paused (click video to resume)")
            self.status_label.setStyleSheet("color: #ffd32a; font-weight: bold;")
        else:
            self.pause_btn.setText("⏸️ PAUSE")
            self.status_label.setText("▶️ Monitoring...")
            self.status_label.setStyleSheet("color: #00d4ff;")

    # ─── DIALOG: DATABASE CONFIG ────────────────────────────────────
    def open_db_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Database Configuration")
        form = QFormLayout()

        host = QLineEdit(self.db_config["host"])
        port = QLineEdit(str(self.db_config["port"]))
        user = QLineEdit(self.db_config["user"])
        pwd = QLineEdit(self.db_config["password"])
        pwd.setEchoMode(QLineEdit.Password)
        db = QLineEdit(self.db_config["database"])

        form.addRow("Host", host)
        form.addRow("Port", port)
        form.addRow("User", user)
        form.addRow("Password", pwd)
        form.addRow("Database", db)

        save_btn = QPushButton("💾 Save & Reconnect")
        save_btn.clicked.connect(lambda: self.handle_save_db_config(dialog, host, port, user, pwd, db))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def handle_save_db_config(self, dialog, host, port, user, pwd, db):
        try:
            self.save_db_config(host.text(), port.text(), user.text(), pwd.text(), db.text())
            self.connect_db()
            dialog.accept()
            QMessageBox.information(self, "Success", "Database config saved!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Invalid input:\n{str(e)}")

    # ─── DIALOG: APP CONFIG ─────────────────────────────────────────
    def open_app_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Application Metadata")
        form = QFormLayout()

        table = QLineEdit(self.app_config["table_name"])
        room = QLineEdit(self.app_config["room_id"])
        device = QLineEdit(self.app_config["device_id"])

        form.addRow("Table Name", table)
        form.addRow("Room ID", room)
        form.addRow("Device ID", device)

        save_btn = QPushButton("💾 Save")
        save_btn.clicked.connect(lambda: self.handle_save_app_config(dialog, table, room, device))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def handle_save_app_config(self, dialog, table, room, device):
        self.save_app_config(table.text(), room.text(), device.text())
        if self.db_conn and self.db_conn.is_connected():
            self.setup_table()
        dialog.accept()
        QMessageBox.information(self, "Success", "App config saved!")

    # ─── VIDEO ──────────────────────────────────────────────────────
    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video Files (*.mp4 *.avi *.mov *.mkv)")
        if path:
            self.video_path = path
            self.vid_label.setText(os.path.basename(path))
            self.vid_label.setStyleSheet("color: #00d4ff;")

        use_live = self.mmwave_checkbox.isChecked() if hasattr(self, "mmwave_checkbox") else False

        if not self.video_path:
            if use_live:
                self.monitoring_armed = True
                self.fall_triggered = False
                self.current_time_str = "00:00"
                self.current_time_sec = 0.0

                self.start_btn.setEnabled(False)
                self.pause_btn.setEnabled(False)   # ga ada video, jadi pause tidak perlu
                self.stop_btn.setEnabled(True)

                self.status_label.setText("🟢 LIVE Monitoring (mmWave) - waiting fall...")
                self.status_label.setStyleSheet("color: #00d4ff;")
                return
        # else:
        #     QMessageBox.warning(self, "Warning", "Select a video first!")
        #     return
            
        self.monitoring_armed = True
        self.fall_triggered = False
        self.last_frame = 0

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

        self.status_label.setText("▶️ Monitoring... (mmWave LIVE + fallback timer)")
        self.status_label.setStyleSheet("color: #00d4ff;")

        total_sec = self.min_spin.value() * 60 + self.sec_spin.value()

        self.video_thread = VideoThread(
            self.video_path,
            total_sec,
            start_frame=self.last_frame,
            fall_already_triggered=self.fall_triggered,
        )
        self.video_thread.change_pixmap.connect(self.update_frame)
        self.video_thread.update_time.connect(self.update_time)
        self.video_thread.update_time_sec.connect(self.update_time_sec)
        self.video_thread.fall_by_timer.connect(lambda: self.trigger_fall(source="fallback_timer", raw_payload={"fall_at_sec": total_sec}))
        self.video_thread.finished.connect(self.on_video_finished)
        self.video_thread.start()
    def start_video(self):
        use_live = self.mmwave_checkbox.isChecked() if hasattr(self, "mmwave_checkbox") else False
        if not self.video_path:
            if use_live:
                # LIVE boleh tanpa video
                self.monitoring_armed = True
                self.fall_triggered = False
                self.current_time_str = "00:00"
                self.current_time_sec = 0.0

                self.start_btn.setEnabled(False)
                self.pause_btn.setEnabled(False)
                self.stop_btn.setEnabled(True)

                self.status_label.setText("🟢 LIVE Monitoring (mmWave) - waiting fall...")
                self.status_label.setStyleSheet("color: #00d4ff;")
                return
            else:
                QMessageBox.warning(self, "Warning", "Select a video first!")
                return

        # ✅ Kalau video ada → play video (HYBRID)
        self.monitoring_armed = True
        self.fall_triggered = False
        self.last_frame = 0

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

        if use_live:
            self.status_label.setText("▶️ Monitoring... (mmWave LIVE + fallback timer)")
        else:
            self.status_label.setText("▶️ Monitoring... (fallback timer only)")
        self.status_label.setStyleSheet("color: #00d4ff;")

        total_sec = self.min_spin.value() * 60 + self.sec_spin.value()

        self.video_thread = VideoThread(
            self.video_path,
            total_sec,
            start_frame=self.last_frame,
            fall_already_triggered=self.fall_triggered,
        )
        self.video_thread.change_pixmap.connect(self.update_frame)
        self.video_thread.update_time.connect(self.update_time)
        self.video_thread.update_time_sec.connect(self.update_time_sec)
        self.video_thread.fall_by_timer.connect(
            lambda: self.trigger_fall(source="fallback_timer", raw_payload={"fall_at_sec": total_sec})
        )
        self.video_thread.finished.connect(self.on_video_finished)
        self.video_thread.start()

    def stop_video(self):
        if self.video_thread and self.video_thread.isRunning():
            self.monitoring_armed = False
            self.video_thread.stop()
            self.video_thread.wait(1000)

        # STOP = reset
        self.last_frame = 0
        self.fall_triggered = False

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸️ PAUSE")
        self.stop_btn.setEnabled(False)

        self.status_label.setText("⏹️ Stopped (reset)")
        self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
        self.time_label.setText("00:00 / 00:00")
        self.current_time_sec = 0.0
        self.monitoring_armed = False
        self.current_time_str = "00:00"

    def on_video_finished(self):
        self.monitoring_armed = False
        self.last_frame = 0
        self.fall_triggered = False
        self.video_thread = None

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸️ PAUSE")
        self.stop_btn.setEnabled(False)

        self.status_label.setText("✅ Monitoring Complete")
        self.status_label.setStyleSheet("color: #00ff00;")

    def update_frame(self, img):
        pixmap = QPixmap.fromImage(img)
        self.video_display.setPixmap(
            pixmap.scaled(self.video_display.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def update_time(self, t):
        self.time_label.setText(f"⏱️ {t}")
        self.current_time_str = t.split("/")[0].strip()

    def update_time_sec(self, s: float):
        self.current_time_sec = s

    # ─── FALL EVENT -> DB (HYBRID) ───────────────────────────────────
    def trigger_fall(self, source="fallback_timer", raw_payload=None):
        # anti double trigger
        if self.fall_triggered:
            return
        self.fall_triggered = True

        # stop timer-trigger in VideoThread
        if self.video_thread:
            self.video_thread.mark_fall_triggered()

        # kalau trigger dari mmWave, otomatis set fall-at spinbox mengikuti waktu sekarang (biar terlihat sinkron)
        if source == "mmwave":
            sec_now = int(self.current_time_sec)
            self.min_spin.setValue(sec_now // 60)
            self.sec_spin.setValue(sec_now % 60)

        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return

        try:
            data = {
                "room_id": self.app_config["room_id"],
                "device_id": self.app_config["device_id"],
                "video_time": self.current_time_str,
                "video_sec": round(float(self.current_time_sec), 3),
                "source": source,  # "mmwave" atau "fallback_timer"
                "fall_status": "FALL DETECTED",
                "raw": raw_payload or {},
            }

            table = self.app_config["table_name"]
            cursor = self.db_conn.cursor()
            cursor.execute(f"INSERT INTO `{table}` (data) VALUES (%s)", (json.dumps(data),))
            cursor.close()

            self.status_label.setText(f"🚨 FALL DETECTED! ({source})")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")

            QMessageBox.warning(
                self,
                "🚨 FALL ALERT",
                f"Source: {source}\nRoom: {data['room_id']}\nDevice: {data['device_id']}\n"
                f"Video time: {data['video_time']}\nSaved to table: {table}",
            )
        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))

    # ─── HISTORY (reads JSON table) ──────────────────────────────────
    def show_history(self):
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("History")
        dialog.resize(900, 500)
        layout = QVBoxLayout()

        tablew = QTableWidget()
        tablew.setColumnCount(3)
        tablew.setHorizontalHeaderLabels(["ID", "Timestamp", "Data(JSON)"])
        tablew.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        tname = self.app_config["table_name"]
        cursor = self.db_conn.cursor()
        cursor.execute(f"SELECT id, timestamp, data FROM `{tname}` ORDER BY id DESC LIMIT 200")
        rows = cursor.fetchall()
        cursor.close()

        tablew.setRowCount(len(rows))
        for i, (rid, ts, data_json) in enumerate(rows):
            tablew.setItem(i, 0, QTableWidgetItem(str(rid)))
            tablew.setItem(i, 1, QTableWidgetItem(str(ts)))
            tablew.setItem(i, 2, QTableWidgetItem(str(data_json)))

        layout.addWidget(tablew)
        dialog.setLayout(layout)
        dialog.exec()

    def closeEvent(self, e):
        if self.video_thread:
            self.video_thread.stop()
            self.video_thread.wait()

        if self.mmwave_thread and self.mmwave_thread.isRunning():
            self.mmwave_thread.stop()
            self.mmwave_thread.wait(1000)

        if self.db_conn and self.db_conn.is_connected():
            self.db_conn.close()
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = FallAlarmTester()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
