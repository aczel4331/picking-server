from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class Ubicacion(Base):
    __tablename__ = "ubicaciones"
    
    id = Column(Integer, primary_key=True, index=True)
    pasillo = Column(String, index=True)
    estanteria = Column(String)
    nivel = Column(String)
    posicion = Column(String)
    
    productos = relationship("Producto", back_populates="ubicacion")

class Producto(Base):
    __tablename__ = "productos"
    
    sku = Column(String, primary_key=True, index=True)
    codigo_interno = Column(String, unique=True, index=True)
    nombre = Column(String)
    ubicacion_id = Column(Integer, ForeignKey("ubicaciones.id"))
    
    ubicacion = relationship("Ubicacion", back_populates="productos")
    items_pedido = relationship("ItemPedido", back_populates="producto")

class Pedido(Base):
    __tablename__ = "pedidos"
    
    order_id = Column(String, primary_key=True, index=True) # ID de Mercado Libre
    cliente = Column(String)
    fecha_compra = Column(DateTime, default=datetime.utcnow)
    estado_envio = Column(String)
    preparado = Column(Boolean, default=False)
    
    items = relationship("ItemPedido", back_populates="pedido")

class ItemPedido(Base):
    __tablename__ = "items_pedido"
    pedido_id = Column(String, ForeignKey("pedidos.order_id"), primary_key=True)
    producto_sku = Column(String, ForeignKey("productos.sku"), primary_key=True)
    cantidad = Column(Integer, default=1)
    
    pedido = relationship("Pedido", back_populates="items")
    producto = relationship("Producto", back_populates="items_pedido")