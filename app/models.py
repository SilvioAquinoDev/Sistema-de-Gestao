from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, Numeric, ForeignKey, Text, Date, JSON, DateTime, Float
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, date
from .database import Base

# SQLAlchemy Models
class NotaFiscal(Base):
    __tablename__ = "notas_fiscais"
    
    id = Column(Integer, primary_key=True, index=True)
    chave_acesso = Column(String(44), unique=True, nullable=False, index=True)
    numero = Column(Integer, nullable=False)
    serie = Column(Integer, default=1)
    data_emissao = Column(TIMESTAMP, nullable=False)
    cnpj_emitente = Column(String(18), nullable=False)
    nome_emitente = Column(String(200), nullable=False)
    ie_emitente = Column(String(20), nullable=True)
    endereco_emitente = Column(Text, nullable=True)
    valor_total = Column(DECIMAL(10, 2), nullable=False)
    cpf_consumidor = Column(String(14), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    
    produtos = relationship("Produto", back_populates="nota_fiscal", cascade="all, delete-orphan")
    pagamentos = relationship("Pagamento", back_populates="nota_fiscal", cascade="all, delete-orphan")


class Produto(Base):
    __tablename__ = "produtos"
    
    id = Column(Integer, primary_key=True, index=True)
    nota_fiscal_id = Column(Integer, ForeignKey("notas_fiscais.id", ondelete="CASCADE"))
    codigo = Column(String(20), nullable=True)
    descricao = Column(String(200), nullable=False)
    unidade = Column(String(5), nullable=True)
    quantidade = Column(DECIMAL(10, 3), nullable=True)
    valor_unitario = Column(DECIMAL(10, 2), nullable=True)
    valor_total = Column(DECIMAL(10, 2), nullable=True)
    
    nota_fiscal = relationship("NotaFiscal", back_populates="produtos")


class Pagamento(Base):
    __tablename__ = "pagamentos"
    
    id = Column(Integer, primary_key=True, index=True)
    nota_fiscal_id = Column(Integer, ForeignKey("notas_fiscais.id", ondelete="CASCADE"))
    forma_pagamento = Column(String(50), nullable=False)
    valor = Column(DECIMAL(10, 2), nullable=False)
    
    nota_fiscal = relationship("NotaFiscal", back_populates="pagamentos")


class LivroDiario(Base):
    """Tabela para o Livro Diário contábil"""
    __tablename__ = "livro_diario"
    
    id = Column(Integer, primary_key=True, index=True)
    data = Column(Date, nullable=False)
    conta = Column(String(100), nullable=False)
    descricao = Column(String(500), nullable=False)
    cliente_fornecedor = Column(String(200), nullable=True)
    entrada = Column(DECIMAL(10, 2), default=0)
    saida = Column(DECIMAL(10, 2), default=0)
    tipo = Column(String(50), default="VENDA")  # VENDA, COMPRA, DESPESA, RECEITA
    nota_fiscal_id = Column(Integer, ForeignKey("notas_fiscais.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    
    nota_fiscal = relationship("NotaFiscal", foreign_keys=[nota_fiscal_id])


class PlanejamentoConfig(Base):
    """Tabela para salvar configurações do planejamento financeiro"""
    __tablename__ = "planejamento_config"
    
    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String(50), nullable=False)  # 'despesas_fixas', 'despesas_variaveis', 'funcionarios', 'geral'
    dados = Column(JSON, nullable=False)  # Armazena os dados em JSON
    ano_referencia = Column(Integer, nullable=False)  # Ano de referência (ex: 2026)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PlanejamentoFaturamento(Base):
    """Tabela para salvar metas de faturamento"""
    __tablename__ = "planejamento_faturamento"
    
    id = Column(Integer, primary_key=True, index=True)
    ano = Column(Integer, nullable=False)
    mes = Column(Integer, nullable=False)  # 1-12
    meta_diaria_almoco = Column(Float, default=0)
    meta_diaria_janta = Column(Float, default=0)
    dias_trabalhados = Column(Integer, default=26)
    lucro_desejado = Column(Float, default=15)  # percentual
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PlanejamentoAcompanhamento(Base):
    """Tabela para salvar acompanhamento mensal (valores reais)"""
    __tablename__ = "planejamento_acompanhamento"
    
    id = Column(Integer, primary_key=True, index=True)
    ano = Column(Integer, nullable=False)
    mes = Column(Integer, nullable=False)
    faturamento_almoco = Column(Float, default=0)
    faturamento_janta = Column(Float, default=0)
    faturamento_total = Column(Float, default=0)
    observacao = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class FichaTecnica(Base):
    __tablename__ = "fichas_tecnicas"
    
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(200), nullable=False)
    categoria = Column(String(50), nullable=False)  # Almoço, Janta
    preco_venda = Column(Numeric(10, 2), nullable=False, default=0)
    custo_total = Column(Numeric(10, 2), nullable=False, default=0)
    margem = Column(Numeric(5, 2), nullable=False, default=0)
    ingredientes = Column(Text, nullable=True)  # JSON string
    modo_preparo = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Pydantic Models para API
class ProdutoBase(BaseModel):
    codigo: Optional[str] = None
    descricao: str
    unidade: Optional[str] = None
    quantidade: Optional[float] = None
    valor_unitario: Optional[float] = None
    valor_total: Optional[float] = None


class PagamentoBase(BaseModel):
    forma_pagamento: str
    valor: float


class NotaFiscalBase(BaseModel):
    chave_acesso: str = Field(..., min_length=44, max_length=44)
    numero: int
    serie: int = 1
    data_emissao: datetime
    cnpj_emitente: str
    nome_emitente: str
    ie_emitente: Optional[str] = None
    endereco_emitente: Optional[str] = None
    valor_total: float
    cpf_consumidor: Optional[str] = None
    produtos: List[ProdutoBase]
    pagamentos: List[PagamentoBase]


class NotaFiscalResponse(NotaFiscalBase):
    id: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class ProcessarNFRequest(BaseModel):
    url: str


class ProcessarNFResponse(BaseModel):
    success: bool
    data: Optional[NotaFiscalBase] = None
    message: str


# Models para Livro Diário
class LivroDiarioBase(BaseModel):
    data: date
    conta: str
    descricao: str
    cliente_fornecedor: Optional[str] = None
    entrada: float = 0
    saida: float = 0
    tipo: str = "VENDA"
    nota_fiscal_id: Optional[int] = None


class LivroDiarioResponse(LivroDiarioBase):
    id: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class LivroDiarioUpdate(BaseModel):
    data: Optional[date] = None
    conta: Optional[str] = None
    descricao: Optional[str] = None
    cliente_fornecedor: Optional[str] = None
    entrada: Optional[float] = None
    saida: Optional[float] = None
    tipo: Optional[str] = None