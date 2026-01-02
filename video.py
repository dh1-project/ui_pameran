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
#RIN
class DbPollThread(QThread): # to monitor DB for fall status per 1 detik
    fall_signal = Signal()
    status_signal = Signal(int, str)  # fall, timestamp

    def __init__(self, db_config, room_id):
        super().__init__()
        self.db_config = db_config
        self.room_id = room_id
        self.running = True
        self.last_seen_ts = None

    def run(self):
        conn = None
        cur = None
        while self.running:
            try:
                if conn is None or not conn.is_connected():
                    conn = mysql.connector.connect(
                        host=self.db_config["host"],
                        port=self.db_config["port"],
                        user=self.db_config["user"],
                        password=self.db_config["password"],
                        database=self.db_config["database"],
                        autocommit=True,
                        connect_timeout=10
                    )
                    cur = conn.cursor()

                # GANTI nama tabel/kolom sesuai DB kamu
                cur.execute("""
                    SELECT fall_detected, updated_at
                    FROM fall_status
                    WHERE room_id=%s
                    ORDER BY updated_at DESC
                    LIMIT 1
                """, (self.room_id,))

                row = cur.fetchone()
                if row:
                    fall, ts = row[0], row[1]
                    ts_str = str(ts)

                    # emit status untuk ditampilkan
                    self.status_signal.emit(int(fall), ts_str)

                    # trigger hanya kalau ada update baru + fall=1
                    if self.last_seen_ts != ts_str and int(fall) == 1:
                        self.fall_signal.emit()
                    self.last_seen_ts = ts_str

            except Exception:
                # kalau error, coba lagi
                pass

            self.msleep(1000)

        try:
            if cur: cur.close()
            if conn: conn.close()
        except:
            pass

    def stop(self):
        self.running = False


class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🚨 Fall Detection System")
        self.resize(1000, 800)
        self.setStyleSheet(MODERN_STYLE)

        self.video_path = None
        self.video_thread = None
        self.db_config = self.load_db_config()
        self.db_conn = None

        self.init_ui()
        self.connect_db()

        #RIN
        self.room_id = "EXECUTIVE-3"   # perbaikan, samakan dengan room_id yang ditulis alat ke DB

        self.db_poll = DbPollThread(self.db_config, self.room_id)
        self.db_poll.fall_signal.connect(self.on_fall_from_device)
        self.db_poll.status_signal.connect(self.on_status_from_device)
        self.db_poll.start()
    

    def load_db_config(self):
        config_file = 'db_config.json'
        default = {'host':'localhost','port':3306,'database':'fall_detection_db','user':'root','password':''}
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    return json.load(f)
            except:
                return default
        with open(config_file, 'w') as f:
            json.dump(default, f)
        return default

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
        cursor.execute('''CREATE TABLE IF NOT EXISTS fall_events (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME,
            video_file VARCHAR(255),
            fall_time VARCHAR(20),
            video_current_time VARCHAR(20),
            status VARCHAR(50)
        )''')
        cursor.close()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Title
        title = QLabel("TEST ALARM FALL DETECTION SYSTEM")
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # DB Status
        db_row = QHBoxLayout()
        self.db_status = QLabel("🔴 Disconnected")
        self.db_status.setObjectName("statusLabel")
        db_row.addWidget(QPushButton("⚙️ Config", clicked=self.open_db_config))
        db_row.addWidget(self.db_status)
        db_row.addWidget(QPushButton("🔄 Reconnect", clicked=self.connect_db))
        layout.addLayout(db_row)

        # Video Selection
        vid_row = QHBoxLayout()
        vid_row.addWidget(QPushButton("📁 Select Video", clicked=self.select_video))
        self.vid_label = QLabel("No video selected")
        self.vid_label.setStyleSheet("color: #aaa;")
        vid_row.addWidget(self.vid_label)
        layout.addLayout(vid_row)

        # Fall Time Input
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("⏰ Fall at:"))
        self.min_spin = QSpinBox(minimum=0, maximum=59, value=1, suffix=" min")
        self.sec_spin = QSpinBox(minimum=0, maximum=59, value=30, suffix=" sec")
        time_row.addWidget(self.min_spin)
        time_row.addWidget(QLabel(":"))
        time_row.addWidget(self.sec_spin)
        time_row.addStretch()
        layout.addLayout(time_row)

        # Control Buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶️ START", clicked=self.start_video)
        self.start_btn.setObjectName("startBtn")
        self.stop_btn = QPushButton("⏹️ STOP", clicked=self.stop_video)
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn, 2)
        btn_row.addWidget(self.stop_btn, 1)
        layout.addLayout(btn_row)

        # Video Display
        self.video_display = QLabel()
        self.video_display.setObjectName("videoFrame")
        self.video_display.setMinimumSize(400, 300)
        self.video_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_display.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.video_display)

        # Status & Time
        self.time_label = QLabel("00:00 / 00:00")
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.time_label)
        layout.addWidget(self.status_label)

        # History Button
        layout.addWidget(QPushButton("View History", clicked=self.show_history))

    def open_db_config(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("DB Config")
        form = QFormLayout()
        host = QLineEdit(self.db_config.get('host', 'localhost'))
        port = QLineEdit(str(self.db_config.get('port', 3306)))
        db = QLineEdit(self.db_config.get('database', 'fall_detection_db'))
        user = QLineEdit(self.db_config.get('user', 'root'))
        pwd = QLineEdit(self.db_config.get('password', ''))
        pwd.setEchoMode(QLineEdit.Password)
        form.addRow("Host", host)
        form.addRow("Port", port)
        form.addRow("DB", db)
        form.addRow("User", user)
        form.addRow("Password", pwd)
        save_btn = QPushButton("Save & Reconnect")
        save_btn.clicked.connect(lambda: self.save_config_and_reconnect(dialog, host, port, db, user, pwd))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def save_config_and_reconnect(self, dialog, host, port, db, user, pwd):
        self.db_config = {
            'host': host.text(),
            'port': int(port.text()),
            'database': db.text(),
            'user': user.text(),
            'password': pwd.text()
        }
        with open('db_config.json', 'w') as f:
            json.dump(self.db_config, f)
        self.connect_db()
        dialog.accept()

    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video Files (*.mp4 *.avi *.mov *.mkv)")
        if path:
            self.video_path = path
            self.vid_label.setText(os.path.basename(path))
            self.vid_label.setStyleSheet("color: #00d4ff;")

    def start_video(self):
        if not self.video_path:
            QMessageBox.warning(self, "video not selected", "Please select a video file before starting.")
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
            self.video_thread.wait()   
            self.on_video_finished()       

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
            now = datetime.now()
            vid = os.path.basename(self.video_path)
            fall_t = f"{self.min_spin.value():02}:{self.sec_spin.value():02}"
            cur_t = self.time_label.text().split(" ")[1].split("/")[0].strip()
            cursor = self.db_conn.cursor()
            cursor.execute(
                "INSERT INTO fall_events (timestamp, video_file, fall_time, video_current_time, status) VALUES (%s,%s,%s,%s,%s)",
                (now, vid, fall_t, cur_t, "FALL DETECTED")
            )
            cursor.close()
            self.status_label.setText("🚨 FALL DETECTED!")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
            QMessageBox.warning(self, "🚨 FALL ALERT", f"Fall at: {fall_t}\nSaved to database.")
        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))

    #RIN
    def on_status_from_device(self, fall, ts):
        if fall == 1:
            self.status_label.setText(f"🚨 FALL dari alat! ({self.room_id}) @ {ts}")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
        else:
            self.status_label.setText(f"🟢 OK ({self.room_id}) @ {ts}")
            self.status_label.setStyleSheet("color: #00ff00;")

    def on_fall_from_device(self):
        if self.video_path and (self.video_thread is None or not self.video_thread.isRunning()):
            self.start_video()
            self.trigger_fall()

    def show_history(self):
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return
             
        dialog = QDialog(self)
        dialog.setWindowTitle("History")
        dialog.resize(900, 500)
        layout = QVBoxLayout()
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["ID", "Time", "Video", "Fall Time", "Status"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT id, timestamp, video_file, fall_time, status FROM fall_events ORDER BY id DESC")
        for i, row in enumerate(cursor.fetchall()):
            table.setRowCount(i + 1)
            for j, val in enumerate(row):
                table.setItem(i, j, QTableWidgetItem(str(val)))
        cursor.close()
        layout.addWidget(table)
        dialog.setLayout(layout)
        dialog.exec()

    def closeEvent(self, e):
        #RIN
        if hasattr(self, "db_poll") and self.db_poll:
            self.db_poll.stop()
            self.db_poll.wait()
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