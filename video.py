# coding: utf-8
import sys
import cv2
import json
import os
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView
)
from PySide6.QtCore import QThread, Signal, Qt, QMutex, QWaitCondition
from PySide6.QtGui import QImage, QPixmap, QFont
import mysql.connector


MODERN_STYLE = """
QMainWindow { background: #1a1a2e; }
QWidget { color: #eaeaea; font-family: 'Segoe UI'; }
QDialog { background: #16213e; }
QDialog QLabel { color: #eaeaea; }
QGroupBox { border: 2px solid #0f3460; border-radius: 8px; padding: 12px; background: rgba(15,52,96,0.3); }
QPushButton { background: #0f3460; border: 2px solid #00d4ff; border-radius: 6px; padding: 8px; color: white; }
QPushButton#startBtn { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #00d4ff,stop:1 #0099cc); }
QPushButton#stopBtn { background: #ff4757; }
QLineEdit, QSpinBox { 
    background: #1a1a2e; 
    border: 2px solid #0f3460; 
    border-radius: 4px; 
    padding: 6px; 
    color: #ffffff; 
}
QLineEdit:focus, QSpinBox:focus { border: 2px solid #00d4ff; }
QLabel#titleLabel { font-size: 22px; color: #00d4ff; font-weight: bold; }
QLabel#statusLabel { font-weight: bold; }
#videoFrame { background: black; border: 2px solid #00d4ff; border-radius: 8px; }
"""


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


class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fall Detection System")
        self.resize(1000, 850)
        self.setStyleSheet(MODERN_STYLE)

        self.video_path = None
        self.video_thread = None
        self.db_config = self.load_db_config()
        self.patient_data = self.load_patient_data()
        self.db_conn = None

        self.last_frame = 0
        self.fall_triggered = False
        self.current_time_str = "00:00"

        self.init_ui()
        self.connect_db()

    def load_db_config(self):
        config_file = "db_config.json"
        default = {"host": "localhost", "port": 3306, "user": "root", "password": "", "database": "darsinurse"}
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in default.items():
                    data.setdefault(k, v)
                return data
            except:
                pass
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
        return default

    def load_patient_data(self):
        return {
            "emr_no": "EMR-001",
            "heart_rate": 72,
            "respirasi": 16,
            "jarak_kasur_cm": 0.0,
            "glukosa": 90.0,
            "berat_badan_kg": 65.0,
            "sistolik": 120,
            "diastolik": 80,
            "tinggi_badan_cm": 170.0,
        }

    def save_db_config(self, host, port, user, password, database):
        self.db_config = {"host": host, "port": int(port), "user": user, "password": password, "database": database}
        with open("db_config.json", "w", encoding="utf-8") as f:
            json.dump(self.db_config, f, indent=4)

    def connect_db(self):
        try:
            if self.db_conn and self.db_conn.is_connected():
                self.db_conn.close()
        except:
            pass

        try:
            self.db_conn = mysql.connector.connect(
                host=self.db_config["host"],
                port=self.db_config["port"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
                autocommit=True,
                connect_timeout=5,
                charset='utf8mb4'
            )
            if self.db_conn.is_connected():
                self.db_status.setText("🟢 Connected")
                self.db_status.setStyleSheet("color: #00ff00;")
                return True
        except Exception as e:
            error_msg = str(e)
            display_msg = error_msg[:50] + "..." if len(error_msg) > 50 else error_msg
            self.db_status.setText(f"🔴 {display_msg}")
            self.db_status.setStyleSheet("color: #ff4757;")
            print(f"[DB ERROR] {e}")
            self.db_conn = None
            return False

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
        db_row.addWidget(QPushButton("📋 Patient Data", clicked=self.open_patient_dialog))
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

        layout.addWidget(QPushButton("View History", clicked=self.show_history))

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

    def open_db_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Database Configuration")
        dialog.setStyleSheet(MODERN_STYLE)
        form = QFormLayout()
        host = QLineEdit(self.db_config["host"])
        port = QLineEdit(str(self.db_config["port"]))
        user = QLineEdit(self.db_config["user"])
        pwd = QLineEdit(self.db_config["password"])
        pwd.setEchoMode(QLineEdit.Password)
        db = QLineEdit(self.db_config["database"])
        for w in (host, port, user, pwd, db):
            w.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")
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

    def open_patient_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Patient Data (EMR)")
        dialog.setStyleSheet(MODERN_STYLE)
        form = QFormLayout()

        fields = [
            ("EMR No", "emr_no", str),
            ("Heart Rate (bpm)", "heart_rate", int),
            ("Respirasi (breaths/min)", "respirasi", int),
            ("Jarak Kasur (cm)", "jarak_kasur_cm", float),
            ("Glukosa (mg/dL)", "glukosa", float),
            ("Berat Badan (kg)", "berat_badan_kg", float),
            ("Tinggi Badan (cm)", "tinggi_badan_cm", float),
            ("Sistolik (mmHg)", "sistolik", int),
            ("Diastolik (mmHg)", "diastolik", int),
        ]

        inputs = {}
        for label, key, _ in fields:
            value = self.patient_data.get(key, "")
            le = QLineEdit(str(value))
            le.setStyleSheet("background: #1a1a2e; color: white; border: 1px solid #0f3460;")
            form.addRow(label, le)
            inputs[key] = le

        save_btn = QPushButton("💾 Save Patient Data")
        save_btn.clicked.connect(lambda: self.save_patient_from_dialog(dialog, inputs, fields))
        form.addRow(save_btn)
        dialog.setLayout(form)
        dialog.exec()

    def save_patient_from_dialog(self, dialog, inputs, fields):
        try:
            data = {}
            for label, key, target_type in fields:
                raw = inputs[key].text()
                if target_type == int:
                    val = int(float(raw))
                elif target_type == float:
                    val = float(raw)
                else:
                    val = raw
                data[key] = val

            # Hitung BMI
            berat = data["berat_badan_kg"]
            tinggi_m = data["tinggi_badan_cm"] / 100
            data["bmi"] = round(berat / (tinggi_m ** 2), 1) if tinggi_m > 0 else 0.0

            self.patient_data = data
            dialog.accept()
            QMessageBox.information(self, "Success", "Patient data updated!")
        except Exception as e:
            QMessageBox.critical(self, "Input Error", f"Invalid value:\n{str(e)}")

    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video Files (*.mp4 *.avi *.mov *.mkv)")
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
            self.video_path, total_sec,
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
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return
        try:
            data = self.patient_data.copy()
            berat = data.get("berat_badan_kg", 0)
            tinggi = data.get("tinggi_badan_cm", 100)
            bmi = round(berat / ((tinggi / 100) ** 2), 1) if tinggi > 0 else 0
            data["bmi"] = bmi

            cursor = self.db_conn.cursor()
            # SIMPAN LANGSUNG KE TABEL vitals (asumsi tabel sudah ada)
            cursor.execute("""
                INSERT INTO vitals (
                    emr_no, heart_rate, respirasi, jarak_kasur_cm, glukosa,
                    berat_badan_kg, sistolik, diastolik, fall_detected,
                    tinggi_badan_cm, bmi
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                data["emr_no"],
                data.get("heart_rate"),
                data.get("respirasi"),
                data.get("jarak_kasur_cm"),
                data.get("glukosa"),
                data.get("berat_badan_kg"),
                data.get("sistolik"),
                data.get("diastolik"),
                True,  # fall_detected
                data.get("tinggi_badan_cm"),
                data.get("bmi")
            ))
            cursor.close()

            self.status_label.setText("FALL DETECTED!")
            self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
            QMessageBox.warning(self, "FALL ALERT", f"Data pasien {data['emr_no']} disimpan ke tabel vitals.")
        except Exception as e:
            print(f"[FALL SAVE ERROR] {e}")
            QMessageBox.critical(self, "DB Error", f"Gagal simpan \n{str(e)}")

    def show_history(self):
        if not (self.db_conn and self.db_conn.is_connected()):
            QMessageBox.critical(self, "Error", "DB not connected!")
            return

        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("Vitals History")
            dialog.resize(1000, 500)
            dialog.setStyleSheet(MODERN_STYLE)
            layout = QVBoxLayout()

            tablew = QTableWidget()
            headers = ["ID", "EMR", "HR", "Resp", "Jarak(cm)", "Glukosa", "Berat(kg)", "Sis", "Dia", "Fall", "Tinggi(cm)", "BMI", "Waktu"]
            tablew.setColumnCount(len(headers))
            tablew.setHorizontalHeaderLabels(headers)
            tablew.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

            cursor = self.db_conn.cursor()
            # BACA DARI TABEL vitals (asumsi sudah ada)
            cursor.execute("""
                SELECT id, emr_no, heart_rate, respirasi, jarak_kasur_cm, glukosa,
                       berat_badan_kg, sistolik, diastolik, fall_detected,
                       tinggi_badan_cm, bmi, created_at
                FROM vitals ORDER BY id DESC LIMIT 100
            """)
            rows = cursor.fetchall()
            cursor.close()

            tablew.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, val in enumerate(row):
                    if j == 9:  # fall_detected
                        display_val = "✅" if val else "❌"
                    else:
                        display_val = str(val) if val is not None else ""
                    tablew.setItem(i, j, QTableWidgetItem(display_val))

            layout.addWidget(tablew)
            dialog.setLayout(layout)
            dialog.exec()
        except Exception as e:
            print(f"[HISTORY ERROR] {e}")
            QMessageBox.critical(self, "Error", f"Gagal muat riwayat:\n{str(e)}")

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