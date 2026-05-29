from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from database import engine, get_db
import models

# Crear las tablas en la BD
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Logística Everest API")

# Esquema Pydantic para formatear y validar la respuesta
class PedidoResponse(BaseModel):
    order_id: str
    cliente: str
    estado_envio: str
    preparado: bool

    class Config:
        from_attributes = True  # Permite mapear desde objetos SQLAlchemy

@app.get("/pedidos/pendientes", response_model=List[PedidoResponse])
def obtener_pedidos_pendientes(db: Session = Depends(get_db)):
    """Retorna los pedidos que aún no han pasado por el proceso de packing."""
    return db.query(models.Pedido).filter(models.Pedido.preparado == False).all()

@app.get("/rutas/picking")
def generar_ruta_optimizada(db: Session = Depends(get_db)):
    # Lógica de agrupamiento y ordenamiento SQL aquí
    pass