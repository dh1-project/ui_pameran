# coding: utf-8
import sys
import cv2
import json
import os
from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget, QHeaderView, QComboBox
)
from PySide6.QtCore import QThread, Signal, Qt, QMutex, QWaitCondition
from PySide6.QtGui import QImage, QPixmap, QFont

import paho.mqtt.client as mqtt


MODERN_STYLE = """
QMainWindow { background: #1a1a2e; }
QWidget { color: #eaeaea; font-family: 'Segoe UI'; }
QDialog { background: #16213e; }
QDialog QLabel { color: #eaeaea; }
QGroupBox { border: 2px solid #0f3460; border-radius: 8px; padding: 12px; background: rgba(15,52,96,0.3); }
QPushButton { background: #0f3460; border: 2px solid #00d4ff; border-radius: 6px; padding: 8px; color: white; }
QPushButton#startBtn { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #00d4ff,stop:1 #0099cc); }
QPushButton#stopBtn { background: #ff4757; }
QLineEdit, QSpinBox, QComboBox { 
    background: #1a1a2e; 
    border: 2px solid #0f3460; 
    border-radius: 4px; 
    padding: 6px; 
    color: #ffffff; 
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 2px solid #00d4ff; }
QLabel#titleLabel { font-size: 22px; color: #00d4ff; font-weight: bold; }
QLabel#statusLabel { font-weight: bold; }
#videoFrame { background: black; border: 2px solid #00d4ff; border-radius: 8px; }
"""


# =========================
# VIDEO THREAD
# =========================
class VideoThread(QThread):
    change_pixmap = Signal(QImage)
    update_time = Signal(str)
    fall_detected = Signal()
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

            if (not self.fall_triggered) and current_time >= self.fall_time_sec:
                self.fall_detected.emit()
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


# =========================
# MAIN WINDOW
# =========================
class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fall Detection System")
        self.resize(1000, 850)
        self.setStyleSheet(MODERN_STYLE)

        self.video_path = None
        self.video_thread = None

        self.mqtt_config = self.load_mqtt_config()
        self.data_config = self.load_data_config()     # untuk topic hitam
        self.rsi_config = self.load_rsi_config()       # untuk topic rsi/data

        self.last_frame = 0
        self.fall_triggered = False
        self.current_time_str = "00:00"

        self.mqtt_client = None
        self.mqtt_connected = False

        self.init_ui()
        self.setup_mqtt()

    # -------------------------
    # LOAD / SAVE CONFIG
    # -------------------------
    def load_mqtt_config(self):
        config_file = "mqtt_config.json"
        default = {
            "broker": "localhost",
            "port": 1883,
            "topic_hitam": "hitam",
            "topic_rsi": "rsi/data",
            "username": "",
            "password": ""
        }
        return self._load_json(config_file, default)

    def load_data_config(self):
        config_file = "data_config.json"
        default = {
            "room_id": "ROOM_01",
            "status": "PEOPLE",
            "nilai_sensor": 0
        }
        data = self._load_json(config_file, default)

        # normalisasi status
        if isinstance(data.get("status"), bool):
            data["status"] = "PEOPLE_FALL" if data["status"] else "PEOPLE"
        if data.get("status") not in ["PEOPLE", "PEOPLE_FALL", "NO_PEOPLE"]:
            data["status"] = "PEOPLE"

        return data

    def load_rsi_config(self):
        config_file = "rsi_config.json"
        default = {
            "device_id": "RSI-001",
            "heart_rate": 72,
            "breath_rate": 16,
            "distance": 0.0
        }
        return self._load_json(config_file, default)

    def _load_json(self, file, default):
        if os.path.exists(file):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in default.items():
                    data.setdefault(k, v)
                return data
            except:
                pass
        with open(file, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
        return default

    def save_mqtt_config(self, broker, port, topic_hitam, topic_rsi, username, password):
        self.mqtt_config = {
            "broker": broker,
            "port": int(port),
            "topic_hitam": topic_hitam,
            "topic_rsi": topic_rsi,
            "username": username,
            "password": password
        }
        with open("mqtt_config.json", "w", encoding="utf-8") as f:
            json.dump(self.mqtt_config, f, indent=4)

    def save_data_config(self, room_id, status, nilai_sensor):
        self.data_config = {
            "room_id": room_id,
            "status": status,
            "nilai_sensor": float(nilai_sensor)
        }
        with open("data_config.json", "w", encoding="utf-8") as f:
            json.dump(self.data_config, f, indent=4)

    def save_rsi_config(self, device_id, heart_rate, breath_rate, distance):
        self.rsi_config = {
            "device_id": device_id,
            "heart_rate": int(float(heart_rate)),
            "breath_rate": int(float(breath_rate)),
            "distance": float(distance),
        }
        with open("rsi_config.json", "w", encoding="utf-8") as f:
            json.dump(self.rsi_config, f, indent=4)

    # -------------------------
    # MQTT SETUP + CALLBACKS
    # -------------------------
    def setup_mqtt(self):
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except:
                pass
            self.mqtt_client = None

        try:
            self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except Exception:
            self.mqtt_client = mqtt.Client()

        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect

        if self.mqtt_config.get("username"):
            self.mqtt_client.username_pw_set(
                self.mqtt_config.get("username", ""),
                self.mqtt_config.get("password", "")
            )

        try:
            self.mqtt_client.connect(
                self.mqtt_config.get("broker", "localhost"),
                int(self.mqtt_config.get("port", 1883)),
                60
            )
            self.mqtt_client.loop_start()
        except Exception as e:
            print(f"[MQTT ERROR] Failed to connect: {e}")
            self.mqtt_connected = False
            self.mqtt_status.setText("🔴 MQTT: Disconnected")
            self.mqtt_status.setStyleSheet("color: #ff4757;")

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.mqtt_connected = True
            self.mqtt_status.setText("🟢 MQTT: Connected")
            self.mqtt_status.setStyleSheet("color: #00ff00;")
        else:
            self.mqtt_connected = False
            self.mqtt_status.setText(f"🔴 MQTT: Connection failed ({reason_code})")
            self.mqtt_status.setStyleSheet("color: #ff4757;")

    def on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.mqtt_connected = False
        self.mqtt_status.setText("🔴 MQTT: Disconnected")
        self.mqtt_status.setStyleSheet("color: #ff4757;")

    def _presence_from_status(self) -> bool:
        # NO_PEOPLE => False, selain itu True
        return self.data_config.get("status") != "NO_PEOPLE"

    def _now_iso(self) -> str:
        # Timestamp ISO (UTC). Kalau mau WIB: hapus timezone.utc
        return datetime.now(timezone.utc).isoformat()

    def publish_alerts(self):
        """
        Publish 2 topics:
        - topic_hitam: status, room_id, nilai_sensor
        - topic_rsi  : device_id, room_id, breath_rate, heart_rate, distance, presence, timestamp
        """
        if not self.mqtt_connected:
            QMessageBox.critical(self, "MQTT Error", "Not connected to MQTT broker!")
            return False

        try:
            payload_hitam = {
                "room_id": self.data_config.get("room_id", "ROOM_01"),
                "status": self.data_config.get("status", "PEOPLE"),
                # "nilai_sensor": self.data_config.get("nilai_sensor", 0.0),
            }

            payload_rsi = {
                "device_id": self.rsi_config.get("device_id", "Dpameran"),
                "room_id": self.data_config.get("room_id", "ROOM_01"),
                "breath_rate": self.rsi_config.get("breath_rate", 16),
                "heart_rate": self.rsi_config.get("heart_rate", 72),
                "distance": self.rsi_config.get("distance", 0.0),
                # "presence": self._presence_from_status(),
                # "timestamp": self._now_iso(),
            }

            r1 = self.mqtt_client.publish(
                self.mqtt_config.get("topic_hitam", "hitam"),
                json.dumps(payload_hitam),
                qos=1
            )
            r2 = self.mqtt_client.publish(
                self.mqtt_config.get("topic_rsi", "rsi/data"),
                json.dumps(payload_rsi),
                qos=1
            )

            return (r1.rc == mqtt.MQTT_ERR_SUCCESS) and (r2.rc == mqtt.MQTT_ERR_SUCCESS)

        except Exception as e:
            print(f"[MQTT ERROR] {e}")
            return False

    # -------------------------
    # UI
    # -------------------------
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel("TEST ALARM FALL DETECTION SYSTEM")
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        mqtt_row = QHBoxLayout()
        self.mqtt_status = QLabel("🔴 MQTT: Disconnected")
        self.mqtt_status.setObjectName("statusLabel")

        mqtt_row.addWidget(QPushButton("⚙️ MQTT Config", clicked=self.open_mqtt_config_dialog))
        mqtt_row.addWidget(QPushButton("📋 Data Config (hitam)", clicked=self.open_data_config_dialog))
        mqtt_row.addWidget(QPushButton("🫀 RSI Config (rsi/data)", clicked=self.open_rsi_config_dialog))
        mqtt_row.addWidget(QPushButton("🔄 Reconnect MQTT", clicked=self.reconnect_mqtt))
        mqtt_row.addWidget(self.mqtt_status)
        layout.addLayout(mqtt_row)

        vid_row = QHBoxLayout()
        vid_row.addWidget(QPushButton("📁 Select Video", clicked=self.select_video))
        self.vid_label = QLabel("No video selected")
        self.vid_label.setStyleSheet("color: #aaa;")
        vid_row.addWidget(self.vid_label)
        layout.addLayout(vid_row)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("⏰ Fall at:"))
        self.min_spin = QSpinBox(minimum=0, maximum=59, value=1, suffix=" min")
        self.sec_spin = QSpinBox(minimum=0, maximum=59, value=30, suffix=" sec")
        time_row.addWidget(self.min_spin)
        time_row.addWidget(QLabel(":"))
        time_row.addWidget(self.sec_spin)
        time_row.addStretch()
        layout.addLayout(time_row)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶️ START", clicked=self.start_video)
        self.start_btn.setObjectName("startBtn")
        self.pause_btn = QPushButton("⏸️ PAUSE", clicked=self.pause_resume_video)
        self.stop_btn = QPushButton("⏹️ STOP", clicked=self.stop_video)
        self.stop_btn.setObjectName("stopBtn")
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setMinimumHeight(42)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
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

        layout.addWidget(QPushButton("📊 View Config", clicked=self.show_all_config))

    def on_video_clicked(self, event):
        if event.button() == Qt.LeftButton:
            self.pause_resume_video()

    def pause_resume_video(self):
        if not self.video_thread or not self.video_thread.isRunning():
            return
        self.video_thread.toggle_pause()
        if self.video_thread.is_paused():
            self.pause_btn.setText("▶️ RESUME")
            self.status_label.setText("⏸️ Paused")
            self.status_label.setStyleSheet("color: #ffd32a;")
        else:
            self.pause_btn.setText("⏸️ PAUSE")
            self.status_label.setText("▶️ Monitoring...")
            self.status_label.setStyleSheet("color: #00d4ff;")

    # -------------------------
    # CONFIG DIALOGS
    # -------------------------
    def open_mqtt_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("MQTT Configuration")
        dialog.setStyleSheet(MODERN_STYLE)
        form = QFormLayout()

        broker = QLineEdit(self.mqtt_config.get("broker", "localhost"))
        port = QLineEdit(str(self.mqtt_config.get("port", 1883)))
        topic_hitam = QLineEdit(self.mqtt_config.get("topic_hitam", "hitam"))
        topic_rsi = QLineEdit(self.mqtt_config.get("topic_rsi", "rsi/data"))
        username = QLineEdit(self.mqtt_config.get("username", ""))
        password = QLineEdit(self.mqtt_config.get("password", ""))
        password.setEchoMode(QLineEdit.Password)

        for w in (broker, port, topic_hitam, topic_rsi, username, password):
            w.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")

        form.addRow("Broker", broker)
        form.addRow("Port", port)
        form.addRow("Topic (hitam)", topic_hitam)
        form.addRow("Topic (rsi/data)", topic_rsi)
        form.addRow("Username", username)
        form.addRow("Password", password)

        save_btn = QPushButton("💾 Save & Reconnect")
        save_btn.clicked.connect(lambda: self.handle_save_mqtt_config(
            dialog, broker, port, topic_hitam, topic_rsi, username, password
        ))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def handle_save_mqtt_config(self, dialog, broker, port, topic_hitam, topic_rsi, username, password):
        try:
            self.save_mqtt_config(
                broker.text().strip(),
                port.text().strip(),
                topic_hitam.text().strip(),
                topic_rsi.text().strip(),
                username.text().strip(),
                password.text()
            )
            self.reconnect_mqtt()
            dialog.accept()
            QMessageBox.information(self, "Success", "MQTT config saved!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Invalid input:\n{str(e)}")

    def open_data_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Data Configuration (hitam)")
        dialog.setStyleSheet(MODERN_STYLE)
        form = QFormLayout()

        room_id = QLineEdit(self.data_config.get("room_id", "ROOM_01"))
        status_combo = QComboBox()
        status_combo.addItems(["PEOPLE", "PEOPLE_FALL", "NO_PEOPLE"])
        status_combo.setCurrentText(self.data_config.get("status", "PEOPLE"))
        nilai_sensor = QLineEdit(str(self.data_config.get("nilai_sensor", 0)))

        room_id.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")
        status_combo.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")
        nilai_sensor.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")

        form.addRow("Room ID", room_id)
        form.addRow("Status", status_combo)
        form.addRow("Nilai Sensor", nilai_sensor)

        save_btn = QPushButton("💾 Save Data Config")
        save_btn.clicked.connect(lambda: self.handle_save_data_config(
            dialog, room_id, status_combo, nilai_sensor
        ))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def handle_save_data_config(self, dialog, room_id, status_combo, nilai_sensor):
        try:
            self.save_data_config(
                room_id.text().strip(),
                status_combo.currentText().strip(),
                nilai_sensor.text().strip()
            )
            dialog.accept()
            QMessageBox.information(self, "Success", "Data config saved!")
        except Exception as e:
            QMessageBox.critical(self, "Input Error", f"Invalid value:\n{str(e)}")

    def open_rsi_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("RSI Configuration (rsi/data)")
        dialog.setStyleSheet(MODERN_STYLE)
        form = QFormLayout()

        device_id = QLineEdit(str(self.rsi_config.get("device_id", "RSI-001")))
        heart_rate = QLineEdit(str(self.rsi_config.get("heart_rate", 72)))
        breath_rate = QLineEdit(str(self.rsi_config.get("breath_rate", 16)))
        distance = QLineEdit(str(self.rsi_config.get("distance", 0.0)))

        for w in (device_id, heart_rate, breath_rate, distance):
            w.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")

        # room_id ditarik dari data_config (biar konsisten), jadi tampil saja
        room_view = QLabel(self.data_config.get("room_id", "ROOM_01"))
        room_view.setStyleSheet("color:#00d4ff; font-weight:bold;")

        form.addRow("Device ID", device_id)
        form.addRow("Room ID (from Data Config)", room_view)
        form.addRow("Heart Rate", heart_rate)
        form.addRow("Breath Rate", breath_rate)
        form.addRow("Distance", distance)

        save_btn = QPushButton("💾 Save RSI Config")
        save_btn.clicked.connect(lambda: self.handle_save_rsi(dialog, device_id, heart_rate, breath_rate, distance))
        form.addRow(save_btn)

        dialog.setLayout(form)
        dialog.exec()

    def handle_save_rsi(self, dialog, device_id, heart_rate, breath_rate, distance):
        try:
            self.save_rsi_config(
                device_id.text().strip(),
                heart_rate.text().strip(),
                breath_rate.text().strip(),
                distance.text().strip()
            )
            dialog.accept()
            QMessageBox.information(self, "Success", "RSI config saved!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Invalid input:\n{str(e)}")

    def show_all_config(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Current Config")
        dialog.resize(520, 420)
        dialog.setStyleSheet(MODERN_STYLE)
        layout = QVBoxLayout()

        all_cfg = {
            "mqtt_config": self.mqtt_config,
            "data_config": self.data_config,
            "rsi_config": self.rsi_config
        }
        text = json.dumps(all_cfg, indent=2)
        label = QLabel(f"<pre>{text}</pre>")
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        label.setStyleSheet("background: #0f3460; padding: 10px; border-radius: 5px;")
        layout.addWidget(label)

        dialog.setLayout(layout)
        dialog.exec()

    def reconnect_mqtt(self):
        self.setup_mqtt()

    # -------------------------
    # VIDEO + FALL TRIGGER
    # -------------------------
    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video", "", "Video Files (*.mp4 *.avi *.mov *.mkv)"
        )
        if path:
            self.video_path = path
            self.vid_label.setText(os.path.basename(path))
            self.vid_label.setStyleSheet("color: #00d4ff;")

    def start_video(self):
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Select a video first!")
            return

        if self.video_thread and self.video_thread.isRunning():
            if self.video_thread.is_paused():
                self.video_thread.toggle_pause()
                self.pause_btn.setText("⏸️ PAUSE")
            return

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("▶️ Monitoring...")
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
        self.video_thread.fall_detected.connect(self.trigger_fall)
        self.video_thread.finished.connect(self.on_video_finished)
        self.video_thread.start()

    def stop_video(self):
        if self.video_thread and self.video_thread.isRunning():
            self.last_frame = self.video_thread.current_frame
            self.fall_triggered = self.video_thread.fall_triggered
            self.video_thread.stop()
            self.video_thread.wait(1000)

        self.last_frame = 0
        self.fall_triggered = False
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸️ PAUSE")
        self.stop_btn.setEnabled(False)
        self.status_label.setText("⏹️ Stopped")
        self.status_label.setStyleSheet("color: #ff4757;")
        self.time_label.setText("00:00 / 00:00")

    def on_video_finished(self):
        self.last_frame = 0
        self.fall_triggered = False
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("✅ Complete")
        self.status_label.setStyleSheet("color: #00ff00;")

    def update_frame(self, img):
        pixmap = QPixmap.fromImage(img)
        self.video_display.setPixmap(pixmap.scaled(
            self.video_display.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        ))

    def update_time(self, t):
        self.time_label.setText(f"⏱️ {t}")
        self.current_time_str = t.split("/")[0].strip()

    def trigger_fall(self):
        success = self.publish_alerts()
        if success:
            presence = self._presence_from_status()
            ts = self._now_iso()
            self.status_label.setText("🚨 FALL DETECTED!")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
            QMessageBox.warning(
                self, "FALL ALERT",
                f"[hitam]\n"
                f"room_id: {self.data_config.get('room_id')}\n"
                f"status: {self.data_config.get('status')}\n"
                f"nilai_sensor: {self.data_config.get('nilai_sensor')}\n\n"
                f"[rsi/data]\n"
                f"device_id: {self.rsi_config.get('device_id')}\n"
                f"room_id: {self.data_config.get('room_id')}\n"
                f"breath_rate: {self.rsi_config.get('breath_rate')}\n"
                f"heart_rate: {self.rsi_config.get('heart_rate')}\n"
                f"distance: {self.rsi_config.get('distance')}\n"
                f"presence: {presence}\n"
                f"timestamp: {ts}"
            )
        else:
            QMessageBox.critical(self, "MQTT Error", "Failed to send MQTT alerts!")

    def closeEvent(self, e):
        if self.video_thread:
            self.video_thread.stop()
            self.video_thread.wait()
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except:
                pass
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = FallAlarmTester()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
