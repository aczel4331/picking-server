import sys
import requests
from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QLabel, QPushButton
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QThread, Signal

API_URL = "http://localhost:8000"

# Un Hilo (Thread) separado para evitar que la interfaz se congele
class WorkerThread(QThread):
    datos_obtenidos = Signal(list)
    error_obtenido = Signal(str)

    def run(self):
        try:
            response = requests.get(f"{API_URL}/pedidos/pendientes", timeout=5)
            if response.status_code == 200:
                self.datos_obtenidos.emit(response.json())
            else:
                self.error_obtenido.emit(f"Error del servidor: {response.status_code}")
        except requests.exceptions.RequestException:
            self.error_obtenido.emit("Error: No se pudo conectar al Backend.")

class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sistema de Picking - Panel de Control")
        self.resize(1024, 768)
        
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        title = QLabel("Dashboard Operativo")
        title.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        layout.addWidget(title)
        
        self.lbl_pendientes = QLabel("Pedidos Pendientes: Calculando...")
        self.lbl_pendientes.setFont(QFont("Segoe UI", 14))
        layout.addWidget(self.lbl_pendientes)
        
        self.btn_refresh = QPushButton("🔄 Actualizar Métricas")
        self.btn_refresh.setFixedSize(200, 40)
        self.btn_refresh.clicked.connect(self.actualizar_metricas)
        layout.addWidget(self.btn_refresh)
        
        widget = QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)
        
        self.actualizar_metricas()

    def actualizar_metricas(self):
        self.lbl_pendientes.setText("Actualizando métricas...")
        self.btn_refresh.setEnabled(False) # Deshabilita el botón mientras carga
        
        self.worker = WorkerThread()
        self.worker.datos_obtenidos.connect(lambda pedidos: self.lbl_pendientes.setText(f"Pedidos Pendientes: {len(pedidos)}"))
        self.worker.error_obtenido.connect(lambda err: self.lbl_pendientes.setText(err))
        self.worker.finished.connect(lambda: self.btn_refresh.setEnabled(True))
        self.worker.start()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DashboardWindow()
    window.show()
    sys.exit(app.exec())