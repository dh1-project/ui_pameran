import sys
import cv2
import json
import os
from datetime import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QLabel, QLineEdit, 
                               QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem,
                               QGroupBox, QFormLayout, QDoubleSpinBox, QDialog,
                               QHeaderView)
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtGui import QImage, QPixmap, QIcon
import mysql.connector
from mysql.connector import Error


class VideoThread(QThread):
    """Thread untuk memutar video"""
    change_pixmap = Signal(QImage)
    update_time = Signal(str)
    fall_detected = Signal()
    finished = Signal()
    
    def __init__(self, video_path, fall_time_minutes):
        super().__init__()
        self.video_path = video_path
        self.fall_time_minutes = fall_time_minutes
        self.is_running = True
        self.fall_triggered = False
        
    def run(self):
        cap = cv2.VideoCapture(self.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fall_time_seconds = self.fall_time_minutes * 60
        
        frame_count = 0
        
        while self.is_running and cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
                
            # Hitung waktu saat ini
            current_time = frame_count / fps
            total_time = total_frames / fps
            
            # Update time label
            current_min = int(current_time // 60)
            current_sec = int(current_time % 60)
            total_min = int(total_time // 60)
            total_sec = int(total_time % 60)
            
            time_str = f"{current_min:02d}:{current_sec:02d} / {total_min:02d}:{total_sec:02d}"
            self.update_time.emit(time_str)
            
            # Cek fall detection
            if not self.fall_triggered and current_time >= fall_time_seconds:
                self.fall_detected.emit()
                self.fall_triggered = True
            
            # Convert frame untuk display
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_frame.shape
            bytes_per_line = ch * w
            qt_image = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
            
            self.change_pixmap.emit(qt_image)
            
            frame_count += 1
            self.msleep(int(1000 / fps))
            
        cap.release()
        self.finished.emit()
        
    def stop(self):
        self.is_running = False


class DatabaseConfigDialog(QDialog):
    """Dialog untuk konfigurasi database"""
    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.setWindowTitle("Konfigurasi Database MySQL")
        self.setModal(True)
        self.resize(450, 400)
        
        self.config = config or {}
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout()
        
        # Form layout
        form_layout = QFormLayout()
        
        self.host_input = QLineEdit(self.config.get('host', 'localhost'))
        self.port_input = QLineEdit(str(self.config.get('port', 3306)))
        self.database_input = QLineEdit(self.config.get('database', 'fall_detection_db'))
        self.user_input = QLineEdit(self.config.get('user', 'root'))
        self.password_input = QLineEdit(self.config.get('password', ''))
        self.password_input.setEchoMode(QLineEdit.Password)
        
        form_layout.addRow("Host:", self.host_input)
        form_layout.addRow("Port:", self.port_input)
        form_layout.addRow("Database:", self.database_input)
        form_layout.addRow("Username:", self.user_input)
        form_layout.addRow("Password:", self.password_input)
        
        layout.addLayout(form_layout)
        
        # Advanced options
        advanced_group = QGroupBox("Opsi Lanjutan")
        advanced_layout = QVBoxLayout()
        
        from PySide6.QtWidgets import QCheckBox
        self.use_ssl = QCheckBox("Gunakan SSL/TLS")
        self.use_ssl.setChecked(False)
        
        self.force_ipv4 = QCheckBox("Force IPv4")
        self.force_ipv4.setChecked(True)
        
        advanced_layout.addWidget(self.use_ssl)
        advanced_layout.addWidget(self.force_ipv4)
        advanced_group.setLayout(advanced_layout)
        layout.addWidget(advanced_group)
        
        # Info label
        info_label = QLabel("💡 Tip: Untuk koneksi remote, pastikan:\n"
                           "• MySQL server mengizinkan remote access\n"
                           "• Firewall tidak memblokir port\n"
                           "• User memiliki akses dari host Anda")
        info_label.setStyleSheet("color: #666; padding: 10px; background-color: #f0f0f0; border-radius: 5px;")
        layout.addWidget(info_label)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        test_btn = QPushButton("🔍 Test Koneksi")
        test_btn.clicked.connect(self.test_connection)
        test_btn.setStyleSheet("background-color: #2196F3; color: white; padding: 8px;")
        
        test_basic_btn = QPushButton("🔧 Test Dasar")
        test_basic_btn.clicked.connect(self.test_basic_connection)
        test_basic_btn.setStyleSheet("background-color: #FF9800; color: white; padding: 8px;")
        
        save_btn = QPushButton("💾 Simpan")
        save_btn.clicked.connect(self.accept)
        save_btn.setStyleSheet("background-color: #4CAF50; color: white; padding: 8px;")
        
        cancel_btn = QPushButton("❌ Batal")
        cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(test_basic_btn)
        button_layout.addWidget(test_btn)
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)
        self.setLayout(layout)
    
    def test_basic_connection(self):
        """Test koneksi dasar tanpa database"""
        try:
            import socket
            
            # Test 1: Cek DNS/Host resolution
            host = self.host_input.text()
            port = int(self.port_input.text())
            
            try:
                ip = socket.gethostbyname(host)
                msg = f"✅ Host ditemukan: {host} → {ip}\n"
            except socket.gaierror:
                QMessageBox.critical(self, "Error", f"❌ Tidak dapat resolve host: {host}\n\nPastikan hostname benar atau coba gunakan IP address.")
                return
            
            # Test 2: Cek port terbuka
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            
            if result == 0:
                msg += f"✅ Port {port} terbuka dan bisa diakses\n\n"
                msg += "Port dapat diakses! Lanjutkan dengan 'Test Koneksi'"
                QMessageBox.information(self, "Test Dasar - Sukses", msg)
            else:
                msg += f"❌ Port {port} tidak dapat diakses\n\n"
                msg += "Kemungkinan penyebab:\n"
                msg += "• MySQL server tidak berjalan\n"
                msg += "• Firewall memblokir port\n"
                msg += "• Host/Port salah\n"
                msg += "• Perlu VPN/SSH tunnel"
                QMessageBox.critical(self, "Test Dasar - Gagal", msg)
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error saat test koneksi:\n{str(e)}")
        
    def test_connection(self):
        try:
            # Prepare connection params
            connection_params = {
                'host': self.host_input.text(),
                'port': int(self.port_input.text()),
                'user': self.user_input.text(),
                'password': self.password_input.text(),
                'connect_timeout': 10,
                'connection_timeout': 10
            }
            
            # Add database if not empty
            if self.database_input.text():
                connection_params['database'] = self.database_input.text()
            
            # SSL configuration
            if not self.use_ssl.isChecked():
                connection_params['ssl_disabled'] = True
            
            conn = mysql.connector.connect(**connection_params)
            
            if conn.is_connected():
                db_info = conn.get_server_info()
                cursor = conn.cursor()
                cursor.execute("SELECT VERSION()")
                version = cursor.fetchone()
                cursor.close()
                conn.close()
                
                QMessageBox.information(self, "✅ Sukses!", 
                                      f"Koneksi berhasil!\n\n"
                                      f"MySQL Server: {db_info}\n"
                                      f"Version: {version[0]}")
        except Error as e:
            error_msg = str(e)
            suggestion = self.get_error_suggestion(error_msg)
                
            QMessageBox.critical(self, "❌ Koneksi Gagal", 
                               f"Error:\n{error_msg}\n\n{suggestion}")
    
    def get_error_suggestion(self, error_msg):
        """Generate suggestion based on error"""
        suggestions = []
        
        if "Lost connection" in error_msg or "communication packet" in error_msg:
            suggestions.append("📌 Server tidak merespons dengan benar")
            suggestions.append("   • Coba tekan 'Test Dasar' untuk cek koneksi jaringan")
            suggestions.append("   • Pastikan MySQL mengizinkan remote connection")
            suggestions.append("   • Cek my.cnf: bind-address = 0.0.0.0")
            
        elif "Access denied" in error_msg:
            suggestions.append("📌 Username/Password salah atau tidak memiliki akses")
            suggestions.append("   • Verifikasi username dan password")
            suggestions.append("   • Pastikan user memiliki grant untuk remote access:")
            suggestions.append("     GRANT ALL ON db.* TO 'user'@'%' IDENTIFIED BY 'pass';")
            
        elif "Unknown database" in error_msg:
            suggestions.append("📌 Database tidak ditemukan")
            suggestions.append("   • Kosongkan field Database untuk test tanpa database")
            suggestions.append("   • Atau buat database dulu: CREATE DATABASE nama_db;")
            
        elif "Can't connect" in error_msg:
            suggestions.append("📌 Tidak dapat terhubung ke server")
            suggestions.append("   • Cek host dan port")
            suggestions.append("   • Pastikan tidak ada firewall yang memblokir")
            suggestions.append("   • Coba gunakan IP address langsung")
            
        elif "SSL" in error_msg or "TLS" in error_msg:
            suggestions.append("📌 Masalah SSL/TLS")
            suggestions.append("   • Coba centang/uncheck 'Gunakan SSL/TLS'")
            
        else:
            suggestions.append("📌 Error tidak dikenal")
            suggestions.append("   • Coba test koneksi dasar terlebih dahulu")
            
        return "\n💡 Saran:\n" + "\n".join(suggestions)
            
    def get_config(self):
        return {
            'host': self.host_input.text(),
            'port': int(self.port_input.text()),
            'database': self.database_input.text(),
            'user': self.user_input.text(),
            'password': self.password_input.text()
        }


class HistoryDialog(QDialog):
    """Dialog untuk menampilkan history"""
    def __init__(self, parent=None, db_connection=None):
        super().__init__(parent)
        self.setWindowTitle("Fall Events History")
        self.resize(900, 500)
        self.db_connection = db_connection
        
        self.setup_ui()
        self.load_data()
        
    def setup_ui(self):
        layout = QVBoxLayout()
        
        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(['ID', 'Timestamp', 'Video File', 
                                               'Fall Time (min)', 'Video Time', 'Status'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        
        layout.addWidget(self.table)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.load_data)
        
        clear_btn = QPushButton("🗑 Clear History")
        clear_btn.clicked.connect(self.clear_history)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        
        button_layout.addWidget(refresh_btn)
        button_layout.addWidget(clear_btn)
        button_layout.addWidget(close_btn)
        
        layout.addLayout(button_layout)
        self.setLayout(layout)
        
    def load_data(self):
        if not self.db_connection or not self.db_connection.is_connected():
            QMessageBox.critical(self, "Error", "Tidak terhubung ke database!")
            return
            
        try:
            cursor = self.db_connection.cursor()
            cursor.execute('''SELECT id, timestamp, video_file, fall_time_minutes, 
                             video_current_time, status FROM fall_events ORDER BY id DESC''')
            rows = cursor.fetchall()
            cursor.close()
            
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    self.table.setItem(i, j, QTableWidgetItem(str(value)))
                    
        except Error as e:
            QMessageBox.critical(self, "Error", f"Gagal mengambil data:\n{str(e)}")
            
    def clear_history(self):
        reply = QMessageBox.question(self, "Confirm", 
                                     "Hapus semua history dari database?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                cursor = self.db_connection.cursor()
                cursor.execute('DELETE FROM fall_events')
                self.db_connection.commit()
                cursor.close()
                QMessageBox.information(self, "Success", "History berhasil dihapus!")
                self.load_data()
            except Error as e:
                QMessageBox.critical(self, "Error", f"Gagal menghapus data:\n{str(e)}")


class FallAlarmTester(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fall Alarm Testing System")
        self.resize(900, 800)
        
        # Variables
        self.video_path = None
        self.video_thread = None
        self.db_connection = None
        self.config_file = 'db_config.json'
        self.db_config = self.load_db_config()
        
        self.setup_ui()
        self.connect_to_database()
        
    def load_db_config(self):
        """Load database configuration"""
        default_config = {
            'host': 'localhost',
            'port': 3306,
            'database': 'fall_detection_db',
            'user': 'root',
            'password': ''
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except:
                return default_config
        else:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            return default_config
            
    def save_db_config(self):
        """Simpan database configuration"""
        with open(self.config_file, 'w') as f:
            json.dump(self.db_config, f, indent=4)
            
    def connect_to_database(self):
        """Koneksi ke MySQL database"""
        try:
            # Coba koneksi dengan berbagai konfigurasi
            connection_params = {
                'host': self.db_config['host'],
                'port': self.db_config['port'],
                'database': self.db_config['database'],
                'user': self.db_config['user'],
                'password': self.db_config['password'],
                'connect_timeout': 10,
                'autocommit': True
            }
            
            # Tambahkan SSL config jika diperlukan untuk remote connection
            if self.db_config['host'] not in ['localhost', '127.0.0.1']:
                connection_params['ssl_disabled'] = True
            
            self.db_connection = mysql.connector.connect(**connection_params)
            
            if self.db_connection.is_connected():
                self.setup_database()
                self.db_status_label.setText("Status: Terhubung ke MySQL")
                self.db_status_label.setStyleSheet("color: green;")
                return True
        except Error as e:
            error_msg = str(e)
            self.db_status_label.setText(f"Status: Error - {error_msg[:30]}...")
            self.db_status_label.setStyleSheet("color: red;")
            
            # Berikan saran berdasarkan error
            suggestion = ""
            if "Lost connection" in error_msg or "communication packet" in error_msg:
                suggestion = "\n\nSaran:\n• Periksa apakah port sudah benar (MySQL biasanya port 3306)\n• Pastikan MySQL server berjalan\n• Cek firewall/network settings"
            elif "Access denied" in error_msg:
                suggestion = "\n\nSaran:\n• Periksa username dan password\n• Pastikan user memiliki akses ke database"
            
            QMessageBox.critical(self, "Database Error", 
                               f"Gagal koneksi ke database:\n{error_msg}{suggestion}")
            return False
            
    def setup_database(self):
        """Setup MySQL database table"""
        try:
            cursor = self.db_connection.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fall_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    timestamp DATETIME,
                    video_file VARCHAR(255),
                    fall_time_minutes DECIMAL(10,2),
                    video_current_time VARCHAR(20),
                    status VARCHAR(50),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            self.db_connection.commit()
            cursor.close()
        except Error as e:
            QMessageBox.critical(self, "Database Error", f"Error membuat tabel: {str(e)}")
            
    def setup_ui(self):
        """Setup UI components"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Database Configuration Group
        db_group = QGroupBox("Database Configuration")
        db_layout = QHBoxLayout()
        
        config_btn = QPushButton("⚙ Konfigurasi Database")
        config_btn.clicked.connect(self.open_db_config)
        
        self.db_status_label = QLabel("Status: Belum terhubung")
        self.db_status_label.setStyleSheet("color: orange;")
        
        reconnect_btn = QPushButton("🔄 Reconnect")
        reconnect_btn.clicked.connect(self.reconnect_database)
        
        db_layout.addWidget(config_btn)
        db_layout.addWidget(self.db_status_label)
        db_layout.addWidget(reconnect_btn)
        db_layout.addStretch()
        
        db_group.setLayout(db_layout)
        main_layout.addWidget(db_group)
        
        # Video Control Group
        control_group = QGroupBox("Kontrol Video")
        control_layout = QVBoxLayout()
        
        # Video selection
        video_layout = QHBoxLayout()
        select_video_btn = QPushButton("Pilih Video")
        select_video_btn.clicked.connect(self.select_video)
        self.video_label = QLabel("Belum ada video dipilih")
        
        video_layout.addWidget(select_video_btn)
        video_layout.addWidget(self.video_label)
        video_layout.addStretch()
        
        control_layout.addLayout(video_layout)
        
        # Fall time setting
        fall_time_layout = QHBoxLayout()
        fall_time_layout.addWidget(QLabel("Waktu Fall (menit):"))
        
        self.fall_time_spin = QDoubleSpinBox()
        self.fall_time_spin.setMinimum(0.1)
        self.fall_time_spin.setMaximum(999.9)
        self.fall_time_spin.setValue(1.0)
        self.fall_time_spin.setSingleStep(0.1)
        
        fall_time_layout.addWidget(self.fall_time_spin)
        fall_time_layout.addStretch()
        
        control_layout.addLayout(fall_time_layout)
        
        # Control buttons
        button_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("▶ Mulai")
        self.start_btn.clicked.connect(self.start_video)
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; padding: 10px;")
        
        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.clicked.connect(self.stop_video)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("background-color: #f44336; color: white; padding: 10px;")
        
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addStretch()
        
        control_layout.addLayout(button_layout)
        control_group.setLayout(control_layout)
        main_layout.addWidget(control_group)
        
        # Video Preview Group
        preview_group = QGroupBox("Video Preview")
        preview_layout = QVBoxLayout()
        
        self.video_display = QLabel()
        self.video_display.setFixedSize(640, 480)
        self.video_display.setStyleSheet("background-color: black;")
        self.video_display.setAlignment(Qt.AlignCenter)
        
        preview_layout.addWidget(self.video_display, alignment=Qt.AlignCenter)
        preview_group.setLayout(preview_layout)
        main_layout.addWidget(preview_group)
        
        # Status Group
        status_group = QGroupBox("Status")
        status_layout = QVBoxLayout()
        
        self.time_label = QLabel("Waktu: 00:00 / 00:00")
        self.time_label.setStyleSheet("font-size: 14px;")
        
        self.status_label = QLabel("Status: Siap")
        self.status_label.setStyleSheet("color: green; font-size: 14px; font-weight: bold;")
        
        status_layout.addWidget(self.time_label)
        status_layout.addWidget(self.status_label)
        
        status_group.setLayout(status_layout)
        main_layout.addWidget(status_group)
        
        # History Group
        history_group = QGroupBox("History Fall Events")
        history_layout = QHBoxLayout()
        
        history_btn = QPushButton("📋 Tampilkan History")
        history_btn.clicked.connect(self.show_history)
        
        history_layout.addWidget(history_btn)
        history_layout.addStretch()
        
        history_group.setLayout(history_layout)
        main_layout.addWidget(history_group)
        
    def reconnect_database(self):
        """Reconnect ke database"""
        if self.db_connection:
            try:
                self.db_connection.close()
            except:
                pass
        self.connect_to_database()
        
    def open_db_config(self):
        """Buka dialog konfigurasi database"""
        dialog = DatabaseConfigDialog(self, self.db_config)
        if dialog.exec():
            self.db_config = dialog.get_config()
            self.save_db_config()
            self.reconnect_database()
            QMessageBox.information(self, "Success", "Konfigurasi berhasil disimpan!")
            
    def select_video(self):
        """Pilih file video"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Pilih Video",
            "",
            "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*.*)"
        )
        if file_path:
            self.video_path = file_path
            self.video_label.setText(os.path.basename(file_path))
            
    def start_video(self):
        """Mulai memutar video"""
        if not self.video_path:
            QMessageBox.warning(self, "Warning", "Silakan pilih video terlebih dahulu!")
            return
            
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Memutar video...")
        self.status_label.setStyleSheet("color: blue; font-size: 14px; font-weight: bold;")
        
        self.video_thread = VideoThread(self.video_path, self.fall_time_spin.value())
        self.video_thread.change_pixmap.connect(self.update_frame)
        self.video_thread.update_time.connect(self.update_time)
        self.video_thread.fall_detected.connect(self.trigger_fall_alarm)
        self.video_thread.finished.connect(self.video_finished)
        self.video_thread.start()
        
    def stop_video(self):
        """Stop video"""
        if self.video_thread:
            self.video_thread.stop()
            
    def video_finished(self):
        """Video selesai diputar"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Status: Selesai")
        self.status_label.setStyleSheet("color: green; font-size: 14px; font-weight: bold;")
        
    def update_frame(self, image):
        """Update video frame"""
        pixmap = QPixmap.fromImage(image)
        scaled_pixmap = pixmap.scaled(640, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.video_display.setPixmap(scaled_pixmap)
        
    def update_time(self, time_str):
        """Update time label"""
        self.time_label.setText(f"Waktu: {time_str}")
        
    def trigger_fall_alarm(self):
        """Trigger alarm fall dan simpan ke database MySQL"""
        if not self.db_connection or not self.db_connection.is_connected():
            QMessageBox.critical(self, "Database Error", "Tidak terhubung ke database!")
            return
            
        try:
            timestamp = datetime.now()
            video_file = os.path.basename(self.video_path)
            
            cursor = self.db_connection.cursor()
            sql = '''
                INSERT INTO fall_events 
                (timestamp, video_file, fall_time_minutes, video_current_time, status, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
            '''
            values = (
                timestamp,
                video_file,
                self.fall_time_spin.value(),
                self.time_label.text().split(": ")[1].split(" / ")[0],
                'FALL DETECTED',
                f'Fall detected in video: {video_file}'
            )
            
            cursor.execute(sql, values)
            self.db_connection.commit()
            cursor.close()
            
            self.status_label.setText("Status: FALL DETECTED! Data disimpan ke MySQL")
            self.status_label.setStyleSheet("color: red; font-size: 14px; font-weight: bold;")
            
            QMessageBox.warning(self, "Fall Detected!", 
                               f"Fall terdeteksi pada menit {self.fall_time_spin.value()}!\n"
                               f"Data telah disimpan ke MySQL database.")
        except Error as e:
            QMessageBox.critical(self, "Database Error", f"Gagal menyimpan data:\n{str(e)}")
            
    def show_history(self):
        """Tampilkan history fall events"""
        if not self.db_connection or not self.db_connection.is_connected():
            QMessageBox.critical(self, "Database Error", "Tidak terhubung ke database!")
            return
            
        dialog = HistoryDialog(self, self.db_connection)
        dialog.exec()
        
    def closeEvent(self, event):
        """Cleanup saat aplikasi ditutup"""
        if self.video_thread:
            self.video_thread.stop()
            self.video_thread.wait()
        if self.db_connection and self.db_connection.is_connected():
            self.db_connection.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = FallAlarmTester()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()