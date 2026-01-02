import sys
import cv2
import json
import os
from datetime import datetime
from PySide6.QtWidgets import *
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QImage, QPixmap, QFont
import mysql.connector
from mysql.connector import Error

MODERN_STYLE = """
QMainWindow { background: #1a1a2e; }
QWidget { color: #eaeaea; font-family: 'Segoe UI'; }
QGroupBox { border: 2px solid #0f3460; border-radius: 8px; padding: 12px; background: rgba(15,52,96,0.3); }
QPushButton { background: #0f3460; border: 2px solid #00d4ff; border-radius: 6px; padding: 8px; color: white; }
QPushButton#startBtn { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #00d4ff,stop:1 #0099cc); }
QPushButton#stopBtn { background: #ff4757; }
QLineEdit, QSpinBox { background: rgba(26,26,46,0.8); border: 2px solid #0f3460; border-radius: 4px; padding: 6px; color: #eaeaea; }
QLabel#titleLabel { font-size: 22px; color: #00d4ff; font-weight: bold; }
QLabel#statusLabel { font-weight: bold; }
#videoFrame { background: black; border: 2px solid #00d4ff; border-radius: 8px; }
"""

class VideoThread(QThread):
    change_pixmap = Signal(QImage)
    update_time = Signal(str)
    fall_detected = Signal()
    finished = Signal()

    def __init__(self, video_path, fall_time_sec):
        super().__init__()
        self.video_path = video_path
        self.fall_time_sec = fall_time_sec
        self.is_running = True
        self.fall_triggered = False

    def run(self):
        cap = cv2.VideoCapture(self.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_count = 0

        while self.is_running and cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            current_time = frame_count / fps
            total_time = total_frames / fps
            cur = f"{int(current_time//60):02}:{int(current_time%60):02}"
            tot = f"{int(total_time//60):02}:{int(total_time%60):02}"
            self.update_time.emit(f"{cur} / {tot}")

            if not self.fall_triggered and current_time >= self.fall_time_sec:
                self.fall_detected.emit()
                self.fall_triggered = True

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qt_img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
            self.change_pixmap.emit(qt_img)

            frame_count += 1
            self.msleep(int(1000 / fps))

        cap.release()
        self.finished.emit()

    def stop(self):
        self.is_running = False


class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🚨 Fall Detection System")
        self.resize(1000, 850)
        self.setStyleSheet(MODERN_STYLE)

        self.video_path = None
        self.video_thread = None
        self.db_config = self.load_db_config()
        self.app_config = self.load_app_config()
        self.db_conn = None

        self.init_ui()
        self.connect_db()

    # ─── LOAD & SAVE: DATABASE CONFIG ───────────────────────────────
    def load_db_config(self):
        config_file = 'db_config.json'
        default = {
            'host': 'localhost',
            'port': 3306,
            'user': 'root',
            'password': '',
            'database': 'fall_detection_db'
        }
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    # Gabungkan dengan default untuk hindari key error
                    for k, v in default.items():
                        if k not in data:
                            data[k] = v
                    return data
            except:
                return default
        # Buat file default jika belum ada
        with open(config_file, 'w') as f:
            json.dump(default, f, indent=4)
        return default

    def save_db_config(self, host, port, user, password, database):
        self.db_config = {
            'host': host,
            'port': int(port),
            'user': user,
            'password': password,
            'database': database
        }
        with open('db_config.json', 'w') as f:
            json.dump(self.db_config, f, indent=4)

    # ─── LOAD & SAVE: APP CONFIG (metadata) ─────────────────────────
    def load_app_config(self):
        config_file = 'app_config.json'
        default = {
            'table_name': 'fall_events',
            'room_id': 'ROOM_01',
            'device_id': 'CAM_001'
        }
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    for k, v in default.items():
                        if k not in data:
                            data[k] = v
                    return data
            except:
                return default
        with open(config_file, 'w') as f:
            json.dump(default, f, indent=4)
        return default

    def save_app_config(self, table_name, room_id, device_id):
        self.app_config = {
            'table_name': table_name,
            'room_id': room_id,
            'device_id': device_id
        }
        with open('app_config.json', 'w') as f:
            json.dump(self.app_config, f, indent=4)

    # ─── DATABASE ───────────────────────────────────────────────────
    def connect_db(self):
        try:
            self.db_conn = mysql.connector.connect(
                host=self.db_config['host'],
                port=self.db_config['port'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                database=self.db_config['database'],
                autocommit=True,
                connect_timeout=10
            )
            if self.db_conn.is_connected():
                self.setup_table()
                self.db_status.setText("🟢 Connected")
                self.db_status.setStyleSheet("color: #00ff00;")
        except Exception as e:
            self.db_status.setText("🔴 Disconnected")
            self.db_status.setStyleSheet("color: #ff4757;")

    def setup_table(self):
        cursor = self.db_conn.cursor()
        table = self.app_config['table_name']
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS `{table}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                data JSON NOT NULL
            ) ENGINE=InnoDB
        """)
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
        self.stop_btn = QPushButton("⏹️ STOP", clicked=self.stop_video)
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn, 2)
        btn_row.addWidget(self.stop_btn, 1)
        layout.addLayout(btn_row)

        self.video_display = QLabel()
        self.video_display.setObjectName("videoFrame")
        self.video_display.setMinimumSize(400, 300)
        self.video_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_display.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.video_display)

        self.time_label = QLabel("00:00 / 00:00")
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.time_label)
        layout.addWidget(self.status_label)

        layout.addWidget(QPushButton("View History", clicked=self.show_history))

    # ─── DIALOG: DATABASE CONFIG ────────────────────────────────────
    def open_db_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Database Configuration")
        form = QFormLayout()

        host = QLineEdit(self.db_config['host'])
        port = QLineEdit(str(self.db_config['port']))
        user = QLineEdit(self.db_config['user'])
        pwd = QLineEdit(self.db_config['password'])
        pwd.setEchoMode(QLineEdit.Password)
        db = QLineEdit(self.db_config['database'])

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

    # ─── DIALOG: APP CONFIG (metadata) ──────────────────────────────
    def open_app_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Application Metadata")
        form = QFormLayout()

        table = QLineEdit(self.app_config['table_name'])
        room = QLineEdit(self.app_config['room_id'])
        device = QLineEdit(self.app_config['device_id'])

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
        # Pastikan tabel dibuat ulang jika nama berubah
        if self.db_conn and self.db_conn.is_connected():
            self.setup_table()
        dialog.accept()
        QMessageBox.information(self, "Success", "App config saved!")

    # ─── VIDEO & FALL DETECTION ─────────────────────────────────────
    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video Files (*.mp4 *.avi *.mov *.mkv)")
        if path:
            self.video_path = path
            self.vid_label.setText(os.path.basename(path))
            self.vid_label.setStyleSheet("color: #00d4ff;")

    def start_video(self):
        if not self.video_path:
            QMessageBox.warning(self, "⚠️ Warning", "Select a video first!")
            return
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("▶️ Monitoring...")
        self.status_label.setStyleSheet("color: #00d4ff;")
        total_sec = self.min_spin.value() * 60 + self.sec_spin.value()
        self.video_thread = VideoThread(self.video_path, total_sec)
        self.video_thread.change_pixmap.connect(self.update_frame)
        self.video_thread.update_time.connect(self.update_time)
        self.video_thread.fall_detected.connect(self.trigger_fall)
        self.video_thread.finished.connect(self.on_video_finished)
        self.video_thread.start()

    def stop_video(self):
        if self.video_thread:
            self.video_thread.stop()

    def on_video_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("✅ Monitoring Complete")
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

    def trigger_fall(self):
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return
        try:
            data = {
                "room_id": self.app_config['room_id'],
                "device_id": self.app_config['device_id'],
                "rr": 0,
                "hr": 0,
                "fall_status": "FALL DETECTED"
            }

            table = self.app_config['table_name']
            cursor = self.db_conn.cursor()
            cursor.execute(f"INSERT INTO `{table}` (data) VALUES (%s)", (json.dumps(data),))
            cursor.close()

            self.status_label.setText("🚨 FALL DETECTED!")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
            QMessageBox.warning(self, "🚨 FALL ALERT", 
                f"Room: {data['room_id']}\nDevice: {data['device_id']}\n"
                f"Saved to table: {table}")
        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))

    def show_history(self):
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("History - JSON Data")
            dialog.resize(900, 500)
            layout = QVBoxLayout()
            table_widget = QTableWidget()
            table_widget.setColumnCount(3)
            table_widget.setHorizontalHeaderLabels(["ID", "Timestamp", "Data (JSON)"])
            table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

            cursor = self.db_conn.cursor()
            table = self.app_config['table_name']
            cursor.execute(f"SELECT id, timestamp, data FROM `{table}` ORDER BY id DESC")
            rows = cursor.fetchall()
            cursor.close()

            table_widget.setRowCount(len(rows))
            for i, (id_, ts, data) in enumerate(rows):
                table_widget.setItem(i, 0, QTableWidgetItem(str(id_)))
                table_widget.setItem(i, 1, QTableWidgetItem(str(ts)))
                table_widget.setItem(i, 2, QTableWidgetItem(str(data)))

            layout.addWidget(table_widget)
            dialog.setLayout(layout)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load history:\n{str(e)}")

    def closeEvent(self, e):
        if self.video_thread:
            self.video_thread.stop()
            self.video_thread.wait()
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