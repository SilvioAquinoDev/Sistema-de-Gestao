from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func, extract
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime, timedelta
from collections import defaultdict
from cachetools import TTLCache
import logging
import hashlib
from sqlalchemy import text
from .auth import verify_credentials

from .database import get_db
from .models import (
    NotaFiscal, Produto, Pagamento, LivroDiario, FichaTecnica,
    ProcessarNFRequest, ProcessarNFResponse, NotaFiscalResponse,
    LivroDiarioBase, LivroDiarioResponse, LivroDiarioUpdate,
    PlanejamentoConfig, PlanejamentoFaturamento, PlanejamentoAcompanhamento
)
from .infosimples_client import InfosimplesClient

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuração de cache
cache_saldos = TTLCache(maxsize=200, ttl=300)  # 200 itens, 5 minutos
cache_produtos = TTLCache(maxsize=50, ttl=600)  # 50 itens, 10 minutos
cache_fichas = TTLCache(maxsize=100, ttl=600)  # 100 itens, 10 minutos

# Rate limiting simples
request_counts = defaultdict(list)

def rate_limit_check(client_ip: str, max_requests: int = 30, time_window: int = 60):
    """Verifica se o cliente excedeu o limite de requisições"""
    now = datetime.now()
    # Limpar requisições antigas
    request_counts[client_ip] = [
        ts for ts in request_counts[client_ip] 
        if (now - ts).total_seconds() < time_window
    ]
    
    if len(request_counts[client_ip]) >= max_requests:
        raise HTTPException(
            status_code=429, 
            detail=f"Limite de {max_requests} requisições por {time_window} segundos excedido"
        )
    
    request_counts[client_ip].append(now)
    return True

router = APIRouter(prefix="/api", tags=["NFC-e"])

# Inicializa o cliente
try:
    infosimples = InfosimplesClient()
    logger.info("✅ Cliente Infosimples inicializado")
except Exception as e:
    logger.error(f"❌ Erro ao inicializar: {e}")
    infosimples = None

@router.post("/processar-nfce", dependencies=[Depends(verify_credentials)])
async def processar_nfce(...):
    ...

@router.post("/livro-diario", dependencies=[Depends(verify_credentials)])
async def criar_lancamento_livro_diario(...):
    ...

# ============= ENDPOINTS NFC-e =============

@router.post("/processar-nfce", response_model=ProcessarNFResponse)
async def processar_nfce(request: ProcessarNFRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Processa uma NFC-e a partir da URL"""
    
    if not infosimples:
        raise HTTPException(status_code=503, detail="Serviço indisponível")
    
    try:
        logger.info(f"Processando URL: {request.url}")
        
        if not infosimples.validar_url(request.url):
            raise HTTPException(status_code=400, detail="URL inválida. Apenas URLs da SEFAZ-PE são aceitas")
        
        dados_nota = infosimples.consultar_nfce(request.url)
        
        if dados_nota.get('is_cancelada', False):
            return ProcessarNFResponse(
                success=False,
                data=None,
                message="Nota Fiscal está cancelada/inutilizada"
            )
        
        nota_existente = db.query(NotaFiscal).filter(
            NotaFiscal.chave_acesso == dados_nota['chave_acesso']
        ).first()
        
        if nota_existente:
            return ProcessarNFResponse(
                success=True,
                data=dados_nota,
                message="Nota fiscal já processada anteriormente"
            )
        
        # Salvar no banco (síncrono para garantir)
        nota_salva = _salvar_nota_db_sync(dados_nota, db)
        
        # Criar lançamento no livro diário
        _criar_lancamento_livro_diario(nota_salva, dados_nota, db)
        
        # Limpar cache após nova nota
        cache_saldos.clear()
        
        return ProcessarNFResponse(
            success=True,
            data=dados_nota,
            message="Nota processada e lançada no Livro Diário com sucesso!"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao processar nota: {e}")
        raise HTTPException(status_code=400, detail=str(e))


def _salvar_nota_db_sync(dados_nota: dict, db: Session) -> NotaFiscal:
    """Salva nota no banco de dados (versão síncrona)"""
    try:
        nota = NotaFiscal(
            chave_acesso=dados_nota['chave_acesso'],
            numero=dados_nota['numero'],
            serie=dados_nota['serie'],
            data_emissao=dados_nota['data_emissao'],
            cnpj_emitente=dados_nota['cnpj_emitente'],
            nome_emitente=dados_nota['nome_emitente'],
            ie_emitente=dados_nota.get('ie_emitente', ''),
            endereco_emitente=dados_nota.get('endereco_emitente', ''),
            valor_total=dados_nota['valor_total'],
            cpf_consumidor=dados_nota.get('cpf_consumidor')
        )
        db.add(nota)
        db.flush()
        
        for produto in dados_nota['produtos']:
            db_produto = Produto(
                nota_fiscal_id=nota.id,
                codigo=produto.get('codigo', ''),
                descricao=produto['descricao'],
                unidade=produto.get('unidade', 'UN'),
                quantidade=produto.get('quantidade', 0),
                valor_unitario=produto.get('valor_unitario', 0),
                valor_total=produto.get('valor_total', 0)
            )
            db.add(db_produto)
        
        for pagamento in dados_nota['pagamentos']:
            db_pagamento = Pagamento(
                nota_fiscal_id=nota.id,
                forma_pagamento=pagamento['forma_pagamento'],
                valor=pagamento['valor']
            )
            db.add(db_pagamento)
        
        db.commit()
        db.refresh(nota)
        logger.info(f"✅ Nota {dados_nota['chave_acesso']} salva")
        return nota
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao salvar: {e}")
        raise


def _criar_lancamento_livro_diario(nota: NotaFiscal, dados_nota: dict, db: Session):
    """Cria lançamento no livro diário a partir da nota processada"""
    try:
        # Determinar a conta com base na forma de pagamento
        conta = "3.1.1 Receita com Vendas"
        if dados_nota.get('pagamentos'):
            primeira_forma = dados_nota['pagamentos'][0]['forma_pagamento'].upper()
            if 'CRÉDITO' in primeira_forma or 'CREDITO' in primeira_forma:
                conta = "3.1.2 Receita com Cartão de Crédito"
            elif 'DÉBITO' in primeira_forma or 'DEBITO' in primeira_forma:
                conta = "3.1.2 Receita com Cartão de Débito"
            elif 'PIX' in primeira_forma:
                conta = "3.1.4 Receita com PIX"
            elif 'IFOOD' in primeira_forma:
                conta = "3.1.3 Receita Ifood"
        
        lancamento = LivroDiario(
            data=dados_nota['data_emissao'].date(),
            conta=conta,
            descricao=f"NFC-e: {dados_nota['nome_emitente']} - Nota {dados_nota['numero']}",
            cliente_fornecedor=dados_nota.get('cpf_consumidor', 'Consumidor'),
            entrada=dados_nota['valor_total'],
            saida=0,
            tipo="VENDA",
            nota_fiscal_id=nota.id
        )
        db.add(lancamento)
        db.commit()
        logger.info(f"✅ Lançamento no Livro Diário criado para nota {nota.id}")
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Erro ao criar lançamento: {e}")


# ============= ENDPOINTS LIVRO DIÁRIO OTIMIZADOS =============

@router.post("/livro-diario", response_model=LivroDiarioResponse)
async def criar_lancamento_livro_diario(lancamento: LivroDiarioBase, db: Session = Depends(get_db)):
    """Cria um novo lançamento no livro diário"""
    try:
        novo_lancamento = LivroDiario(
            data=lancamento.data,
            conta=lancamento.conta,
            descricao=lancamento.descricao,
            cliente_fornecedor=lancamento.cliente_fornecedor,
            entrada=lancamento.entrada,
            saida=lancamento.saida,
            tipo=lancamento.tipo,
            nota_fiscal_id=lancamento.nota_fiscal_id
        )
        db.add(novo_lancamento)
        db.commit()
        db.refresh(novo_lancamento)
        
        # Limpar cache após novo lançamento
        cache_saldos.clear()
        
        return novo_lancamento
        
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao criar lançamento: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/livro-diario", response_model=List[LivroDiarioResponse])
async def listar_livro_diario(
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    conta: Optional[str] = None,
    tipo: Optional[str] = None,
    skip: int = 0,
    limit: int = 1000,
    db: Session = Depends(get_db)
):
    """Lista os lançamentos do livro diário com filtros (com paginação otimizada)"""
    query = db.query(LivroDiario)
    
    if data_inicio:
        query = query.filter(LivroDiario.data >= data_inicio)
    if data_fim:
        query = query.filter(LivroDiario.data <= data_fim)
    if conta:
        safe_term = conta.replace('%', '%%').replace('_', '__')
        query = query.filter(LivroDiario.conta.ilike(f"%{safe_term}%"))
    if tipo:
        query = query.filter(LivroDiario.tipo == tipo)
    
    lancamentos = query.order_by(LivroDiario.data.desc()).offset(skip).limit(limit).all()
    return lancamentos


@router.get("/livro-diario/{lancamento_id}", response_model=LivroDiarioResponse)
async def obter_lancamento(lancamento_id: int, db: Session = Depends(get_db)):
    """Obtém um lançamento específico do livro diário"""
    lancamento = db.query(LivroDiario).filter(LivroDiario.id == lancamento_id).first()
    if not lancamento:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    return lancamento


@router.put("/livro-diario/{lancamento_id}", response_model=LivroDiarioResponse)
async def atualizar_lancamento(
    lancamento_id: int,
    lancamento_update: LivroDiarioUpdate,
    db: Session = Depends(get_db)
):
    """Atualiza um lançamento existente no livro diário"""
    lancamento = db.query(LivroDiario).filter(LivroDiario.id == lancamento_id).first()
    if not lancamento:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    
    update_data = lancamento_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(lancamento, field, value)
    
    db.commit()
    db.refresh(lancamento)
    
    # Limpar cache após atualização
    cache_saldos.clear()
    
    return lancamento


@router.delete("/livro-diario/{lancamento_id}")
async def deletar_lancamento(lancamento_id: int, db: Session = Depends(get_db)):
    """Remove um lançamento do livro diário"""
    lancamento = db.query(LivroDiario).filter(LivroDiario.id == lancamento_id).first()
    if not lancamento:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    
    db.delete(lancamento)
    db.commit()
    
    # Limpar cache após deleção
    cache_saldos.clear()
    
    return {"message": "Lançamento removido com sucesso"}


@router.post("/livro-diario/lancamento-manual")
async def lancar_movimentacao_manual(
    data: date,
    conta: str,
    descricao: str,
    valor: float,
    tipo: str,
    cliente_fornecedor: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Lançamento manual rápido no livro diário"""
    try:
        lancamento = LivroDiario(
            data=data,
            conta=conta,
            descricao=descricao,
            cliente_fornecedor=cliente_fornecedor or "Manual",
            entrada=valor if tipo.upper() == "ENTRADA" else 0,
            saida=valor if tipo.upper() == "SAIDA" else 0,
            tipo="MANUAL"
        )
        db.add(lancamento)
        db.commit()
        db.refresh(lancamento)
        
        # Limpar cache após novo lançamento
        cache_saldos.clear()
        
        return {"success": True, "message": "Lançamento realizado com sucesso!", "data": lancamento}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/livro-diario/resumo/saldo")
async def obter_saldo_livro_diario_otimizado(
    request: Request,
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """
    Obtém resumo de saldos do livro diário usando SQL aggregation.
    Com cache e rate limiting para proteção.
    """
    # Rate limiting por IP
    client_ip = request.client.host
    rate_limit_check(client_ip, max_requests=30, time_window=60)
    
    # Gerar chave de cache única
    cache_key = f"saldo_{data_inicio}_{data_fim}"
    
    # Verificar cache
    if cache_key in cache_saldos:
        logger.info(f"✅ Cache hit para {cache_key} - IP: {client_ip}")
        return cache_saldos[cache_key]
    
    logger.info(f"💾 Cache miss para {cache_key} - Processando query otimizada")
    
    try:
        # Query otimizada - calcula tudo no banco de dados
        query = db.query(
            func.coalesce(func.sum(LivroDiario.entrada), 0).label('total_entradas'),
            func.coalesce(func.sum(LivroDiario.saida), 0).label('total_saidas'),
            func.count(LivroDiario.id).label('total_lancamentos')
        )
        
        if data_inicio:
            query = query.filter(LivroDiario.data >= data_inicio)
        if data_fim:
            query = query.filter(LivroDiario.data <= data_fim)
        
        resultado = query.first()
        
        total_entradas = float(resultado.total_entradas)
        total_saidas = float(resultado.total_saidas)
        saldo_atual = total_entradas - total_saidas
        
        response_data = {
            "total_entradas": total_entradas,
            "total_saidas": total_saidas,
            "saldo_atual": saldo_atual,
            "total_lancamentos": resultado.total_lancamentos,
            "periodo": {
                "data_inicio": data_inicio.isoformat() if data_inicio else None,
                "data_fim": data_fim.isoformat() if data_fim else None
            },
            "cached": False,
            "cache_expira_em": (datetime.now() + timedelta(seconds=300)).isoformat()
        }
        
        # Salvar no cache
        cache_saldos[cache_key] = response_data
        
        return response_data
        
    except Exception as e:
        logger.error(f"Erro ao obter saldo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/livro-diario/resumo/diario")
async def obter_resumo_diario_otimizado(
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """
    Obtém resumo diário usando GROUP BY no banco - MUITO MAIS RÁPIDO
    """
    try:
        query = db.query(
            LivroDiario.data,
            func.coalesce(func.sum(LivroDiario.entrada), 0).label('entradas'),
            func.coalesce(func.sum(LivroDiario.saida), 0).label('saidas')
        )
        
        if data_inicio:
            query = query.filter(LivroDiario.data >= data_inicio)
        if data_fim:
            query = query.filter(LivroDiario.data <= data_fim)
        
        resultados = query.group_by(LivroDiario.data).order_by(LivroDiario.data).all()
        
        return [
            {
                "data": r.data.isoformat(),
                "entradas": float(r.entradas),
                "saidas": float(r.saidas),
                "saldo_dia": float(r.entradas - r.saidas)
            }
            for r in resultados
        ]
        
    except Exception as e:
        logger.error(f"Erro ao obter resumo diário: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/resumo-completo")
async def obter_dashboard_completo(
    request: Request,
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """
    Endpoint unificado para dashboard - reduz múltiplas chamadas.
    Retorna todas as informações necessárias para o dashboard em uma única requisição.
    """
    # Rate limiting mais restritivo para dashboard
    client_ip = request.client.host
    rate_limit_check(client_ip, max_requests=10, time_window=60)
    
    # Período padrão: último mês
    if not data_fim:
        data_fim = date.today()
    if not data_inicio:
        data_inicio = data_fim - timedelta(days=30)
    
    cache_key = f"dashboard_completo_{data_inicio}_{data_fim}"
    
    if cache_key in cache_saldos:
        logger.info(f"✅ Dashboard cache hit para {cache_key}")
        return cache_saldos[cache_key]
    
    logger.info(f"💾 Dashboard cache miss - Processando dados")
    
    try:
        # 1. Resumo geral
        resumo = db.query(
            func.coalesce(func.sum(LivroDiario.entrada), 0).label('total_entradas'),
            func.coalesce(func.sum(LivroDiario.saida), 0).label('total_saidas'),
            func.count(LivroDiario.id).label('total_lancamentos')
        ).filter(
            LivroDiario.data.between(data_inicio, data_fim)
        ).first()
        
        # 2. Top 5 contas com maior movimentação
        top_contas = db.query(
            LivroDiario.conta,
            func.sum(LivroDiario.entrada + LivroDiario.saida).label('total_movimentado')
        ).filter(
            LivroDiario.data.between(data_inicio, data_fim)
        ).group_by(LivroDiario.conta).order_by(
            func.sum(LivroDiario.entrada + LivroDiario.saida).desc()
        ).limit(5).all()
        
        # 3. Evolução diária (últimos 7 dias)
        evolucao = db.query(
            LivroDiario.data,
            func.sum(LivroDiario.entrada - LivroDiario.saida).label('saldo_dia')
        ).filter(
            LivroDiario.data.between(data_fim - timedelta(days=7), data_fim)
        ).group_by(LivroDiario.data).order_by(LivroDiario.data).all()
        
        response = {
            "periodo": {
                "inicio": data_inicio.isoformat(),
                "fim": data_fim.isoformat(),
                "dias": (data_fim - data_inicio).days
            },
            "resumo": {
                "total_entradas": float(resumo.total_entradas),
                "total_saidas": float(resumo.total_saidas),
                "saldo": float(resumo.total_entradas - resumo.total_saidas),
                "total_lancamentos": resumo.total_lancamentos,
                "media_diaria": float(resumo.total_entradas - resumo.total_saidas) / max(1, (data_fim - data_inicio).days)
            },
            "top_contas": [
                {"conta": conta[0], "total_movimentado": float(conta[1])}
                for conta in top_contas
            ],
            "evolucao_saldo": [
                {"data": e.data.isoformat(), "saldo": float(e.saldo_dia)}
                for e in evolucao
            ],
            "gerado_em": datetime.now().isoformat(),
            "cache_valido_ate": (datetime.now() + timedelta(seconds=300)).isoformat()
        }
        
        # Salvar no cache
        cache_saldos[cache_key] = response
        return response
        
    except Exception as e:
        logger.error(f"Erro ao obter dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============= ENDPOINTS PRODUTOS OTIMIZADOS =============

@router.get("/produtos")
async def listar_produtos(
    request: Request,
    skip: int = 0, 
    limit: int = 100,
    unicos: bool = True,
    db: Session = Depends(get_db)
):
    """
    Lista produtos das notas fiscais processadas (com cache)
    """
    # Rate limiting
    client_ip = request.client.host
    rate_limit_check(client_ip, max_requests=50, time_window=60)
    
    cache_key = f"produtos_{skip}_{limit}_{unicos}"
    
    if cache_key in cache_produtos:
        return cache_produtos[cache_key]
    
    try:
        query = db.query(Produto)
        
        if unicos:
            # Buscar produtos únicos por descrição
            subquery = db.query(
                Produto.descricao,
                func.max(Produto.id).label('max_id')
            ).group_by(Produto.descricao).subquery()
            
            produtos = db.query(Produto).join(
                subquery,
                Produto.id == subquery.c.max_id
            ).order_by(Produto.descricao).offset(skip).limit(limit).all()
        else:
            produtos = query.order_by(Produto.descricao).offset(skip).limit(limit).all()
        
        resultado = []
        for produto in produtos:
            resultado.append({
                'id': produto.id,
                'codigo': produto.codigo,
                'nome': produto.descricao,
                'descricao': produto.descricao,
                'preco_venda': float(produto.valor_unitario) if produto.valor_unitario else 0,
                'unidade': produto.unidade,
                'nota_fiscal_id': produto.nota_fiscal_id
            })
        
        cache_produtos[cache_key] = resultado
        return resultado
        
    except Exception as e:
        logger.error(f"Erro ao listar produtos: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/produtos/{produto_id}")
async def obter_produto(produto_id: int, db: Session = Depends(get_db)):
    """Obtém um produto específico pelo ID"""
    try:
        produto = db.query(Produto).filter(Produto.id == produto_id).first()
        if not produto:
            raise HTTPException(status_code=404, detail="Produto não encontrado")
        
        return {
            'id': produto.id,
            'codigo': produto.codigo,
            'nome': produto.descricao,
            'descricao': produto.descricao,
            'preco_venda': float(produto.valor_unitario) if produto.valor_unitario else 0,
            'unidade': produto.unidade,
            'quantidade': float(produto.quantidade) if produto.quantidade else 0,
            'valor_total': float(produto.valor_total) if produto.valor_total else 0,
            'nota_fiscal_id': produto.nota_fiscal_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao obter produto: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@router.post("/produtos/batch")
async def criar_produtos_em_lote(
    produtos: List[dict],
    db: Session = Depends(get_db)
):
    """Cria múltiplos produtos de uma vez (útil para importação)"""
    try:
        produtos_criados = []
        for prod_data in produtos:
            produto = Produto(
                codigo=prod_data.get('codigo', ''),
                descricao=prod_data['descricao'],
                unidade=prod_data.get('unidade', 'UN'),
                quantidade=prod_data.get('quantidade', 0),
                valor_unitario=prod_data.get('valor_unitario', 0),
                valor_total=prod_data.get('valor_total', 0),
                nota_fiscal_id=prod_data.get('nota_fiscal_id')
            )
            db.add(produto)
            db.flush()
            produtos_criados.append({
                'id': produto.id,
                'descricao': produto.descricao
            })
        
        db.commit()
        
        # Limpar cache de produtos
        cache_produtos.clear()
        
        return {"success": True, "message": f"{len(produtos_criados)} produtos criados", "produtos": produtos_criados}
        
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao criar produtos em lote: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/produtos/categorias")
async def listar_categorias_produtos(db: Session = Depends(get_db)):
    """Lista categorias/tipos de produtos (primeiras palavras da descrição)"""
    try:
        produtos = db.query(Produto.descricao).all()
        
        categorias = set()
        for produto in produtos:
            if produto.descricao:
                primeira_palavra = produto.descricao.split()[0] if produto.descricao.split() else produto.descricao
                categorias.add(primeira_palavra)
        
        return sorted(list(categorias))
        
    except Exception as e:
        logger.error(f"Erro ao listar categorias: {e}")
        return []


# ============= ENDPOINTS NOTAS =============

@router.get("/notas", response_model=List[NotaFiscalResponse])
async def listar_notas(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    notas = db.query(NotaFiscal).offset(skip).limit(limit).all()
    
    resultado = []
    for nota in notas:
        resultado.append({
            'id': nota.id,
            'chave_acesso': nota.chave_acesso,
            'numero': nota.numero,
            'serie': nota.serie,
            'data_emissao': nota.data_emissao,
            'cnpj_emitente': nota.cnpj_emitente,
            'nome_emitente': nota.nome_emitente,
            'ie_emitente': nota.ie_emitente,
            'endereco_emitente': nota.endereco_emitente,
            'valor_total': float(nota.valor_total),
            'cpf_consumidor': nota.cpf_consumidor,
            'produtos': [{'codigo': p.codigo, 'descricao': p.descricao, 'unidade': p.unidade,
                         'quantidade': float(p.quantidade) if p.quantidade else None,
                         'valor_unitario': float(p.valor_unitario) if p.valor_unitario else None,
                         'valor_total': float(p.valor_total) if p.valor_total else None} for p in nota.produtos],
            'pagamentos': [{'forma_pagamento': pg.forma_pagamento, 'valor': float(pg.valor)} for pg in nota.pagamentos],
            'created_at': nota.created_at
        })
    
    return resultado


@router.get("/health")
async def health_check():
    return {
        "status": "ok", 
        "infosimples": "configured" if infosimples and infosimples.token else "no_token",
        "cache": {
            "saldos": len(cache_saldos),
            "produtos": len(cache_produtos),
            "fichas": len(cache_fichas)
        }
    }


@router.get("/metrics/performance")
async def obter_metrics_performance():
    """Endpoint para monitorar performance do sistema"""
    return {
        "cache": {
            "saldos": {
                "tamanho": len(cache_saldos),
                "max_size": cache_saldos.maxsize,
                "chaves": list(cache_saldos.keys())[:10]  # Mostra apenas as 10 primeiras
            },
            "produtos": {
                "tamanho": len(cache_produtos),
                "max_size": cache_produtos.maxsize
            },
            "fichas": {
                "tamanho": len(cache_fichas),
                "max_size": cache_fichas.maxsize
            }
        },
        "rate_limiting": {
            "clientes_ativos": len(request_counts),
            "limite_por_minuto": 30,
            "time_window_segundos": 60
        },
        "timestamp": datetime.now().isoformat()
    }


@router.post("/cache/clear")
async def limpar_cache():
    """Endpoint administrativo para limpar todo o cache"""
    cache_saldos.clear()
    cache_produtos.clear()
    cache_fichas.clear()
    return {"message": "Cache limpo com sucesso", "timestamp": datetime.now().isoformat()}


# ============= ENDPOINTS PLANEJAMENTO =============

def get_dados_padrao_planejamento(tipo: str):
    """Retorna dados padrão para cada tipo de configuração"""
    
    if tipo == "despesas_fixas":
        return [
            {"nome": "ALUGUEL", "valor": 1200},
            {"nome": "CELPE", "valor": 700},
            {"nome": "COMPESA", "valor": 310},
            {"nome": "TELEFONE", "valor": 112},
            {"nome": "INTERNET", "valor": 70},
            {"nome": "CONTABILIDADE", "valor": 350},
            {"nome": "SOFTWARE GESTAO", "valor": 144.4},
            {"nome": "MANUT. BANCOS", "valor": 99},
            {"nome": "PASSAGEM FUNCIN.", "valor": 635},
            {"nome": "INSS", "valor": 446},
            {"nome": "MERCANTIL", "valor": 200},
            {"nome": "MAQUINETAS", "valor": 120},
            {"nome": "CARRO", "valor": 0},
            {"nome": "COMBUSTIVEL", "valor": 200},
            {"nome": "BOMBEIROS", "valor": 30},
            {"nome": "IPTU", "valor": 150},
            {"nome": "ANOTAI", "valor": 0},
            {"nome": "GAS", "valor": 1330},
            {"nome": "CELULAR", "valor": 20},
            {"nome": "PRO-LABORE", "valor": 1500}
        ]
    
    elif tipo == "despesas_variaveis":
        return [
            {"nome": "Simples Nacional", "percentual": 8.0},
            {"nome": "Taxa Cartão Débito", "percentual": 0.2},
            {"nome": "Taxa Cartão Crédito", "percentual": 0.15},
            {"nome": "Manutenção Equipamento", "percentual": 1.0},
            {"nome": "Taxa Ifood", "percentual": 0.4},
            {"nome": "Taxa Voucher", "percentual": 0.12},
            {"nome": "Taxa 99Food", "percentual": 0.0}
        ]
    
    elif tipo == "funcionarios":
        return [
            {"nome": "Sandra", "salario": 1302},
            {"nome": "Lene", "salario": 1302},
            {"nome": "Marilia", "salario": 1302},
            {"nome": "Meiry", "salario": 1500},
            {"nome": "Diarista", "salario": 1920}
        ]
    
    else:
        return []


@router.get("/planejamento/config/{tipo}")
async def get_planejamento_config(
    tipo: str,
    ano: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Obtém configuração do planejamento por tipo"""
    if ano is None:
        ano = datetime.now().year
    
    config = db.query(PlanejamentoConfig).filter(
        PlanejamentoConfig.tipo == tipo,
        PlanejamentoConfig.ano_referencia == ano
    ).first()
    
    if config:
        return {
            "success": True,
            "tipo": config.tipo,
            "dados": config.dados,
            "ano": config.ano_referencia,
            "updated_at": config.updated_at
        }
    else:
        dados_padrao = get_dados_padrao_planejamento(tipo)
        return {
            "success": True,
            "tipo": tipo,
            "dados": dados_padrao,
            "ano": ano,
            "is_default": True
        }


@router.post("/planejamento/config/{tipo}")
async def save_planejamento_config(
    tipo: str,
    dados: dict,
    ano: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Salva configuração do planejamento"""
    if ano is None:
        ano = datetime.now().year
    
    config_existente = db.query(PlanejamentoConfig).filter(
        PlanejamentoConfig.tipo == tipo,
        PlanejamentoConfig.ano_referencia == ano
    ).first()
    
    if config_existente:
        config_existente.dados = dados
        config_existente.updated_at = func.now()
        db.commit()
        db.refresh(config_existente)
        return {"success": True, "message": "Configuração atualizada", "id": config_existente.id}
    else:
        nova_config = PlanejamentoConfig(
            tipo=tipo,
            dados=dados,
            ano_referencia=ano
        )
        db.add(nova_config)
        db.commit()
        db.refresh(nova_config)
        return {"success": True, "message": "Configuração salva", "id": nova_config.id}


@router.get("/planejamento/faturamento-meta")
async def get_faturamento_meta(
    ano: Optional[int] = None,
    mes: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Obtém metas de faturamento"""
    if ano is None:
        ano = datetime.now().year
    
    query = db.query(PlanejamentoFaturamento).filter(
        PlanejamentoFaturamento.ano == ano
    )
    
    if mes:
        query = query.filter(PlanejamentoFaturamento.mes == mes)
        resultado = query.first()
        if resultado:
            return {
                "success": True,
                "ano": resultado.ano,
                "mes": resultado.mes,
                "meta_diaria_almoco": resultado.meta_diaria_almoco,
                "meta_diaria_janta": resultado.meta_diaria_janta,
                "dias_trabalhados": resultado.dias_trabalhados,
                "lucro_desejado": resultado.lucro_desejado
            }
        else:
            return {
                "success": True,
                "ano": ano,
                "mes": mes,
                "meta_diaria_almoco": 0,
                "meta_diaria_janta": 0,
                "dias_trabalhados": 26,
                "lucro_desejado": 15,
                "is_default": True
            }
    else:
        resultados = query.order_by(PlanejamentoFaturamento.mes).all()
        return {
            "success": True,
            "ano": ano,
            "metas": [
                {
                    "mes": r.mes,
                    "meta_diaria_almoco": r.meta_diaria_almoco,
                    "meta_diaria_janta": r.meta_diaria_janta,
                    "dias_trabalhados": r.dias_trabalhados,
                    "lucro_desejado": r.lucro_desejado
                }
                for r in resultados
            ]
        }


@router.post("/planejamento/faturamento-meta")
async def save_faturamento_meta_corrigido(
    request: Request,
    db: Session = Depends(get_db)
):
    """Salva meta de faturamento aceitando JSON no body"""
    try:
        # Tentar ler JSON do body
        body = await request.json()
        
        ano = body.get('ano')
        mes = body.get('mes')
        meta_diaria_almoco = body.get('meta_diaria_almoco')
        meta_diaria_janta = body.get('meta_diaria_janta')
        dias_trabalhados = body.get('dias_trabalhados', 26)
        lucro_desejado = body.get('lucro_desejado', 15)
        
        # Validar campos obrigatórios
        if not all([ano, mes, meta_diaria_almoco is not None, meta_diaria_janta is not None]):
            raise HTTPException(
                status_code=400, 
                detail="Campos obrigatórios: ano, mes, meta_diaria_almoco, meta_diaria_janta"
            )
        
        # Verificar se já existe
        meta_existente = db.query(PlanejamentoFaturamento).filter(
            PlanejamentoFaturamento.ano == ano,
            PlanejamentoFaturamento.mes == mes
        ).first()
        
        if meta_existente:
            meta_existente.meta_diaria_almoco = float(meta_diaria_almoco)
            meta_existente.meta_diaria_janta = float(meta_diaria_janta)
            meta_existente.dias_trabalhados = dias_trabalhados
            meta_existente.lucro_desejado = lucro_desejado
            db.commit()
            db.refresh(meta_existente)
            return {"success": True, "message": "Meta atualizada", "id": meta_existente.id}
        else:
            nova_meta = PlanejamentoFaturamento(
                ano=ano,
                mes=mes,
                meta_diaria_almoco=float(meta_diaria_almoco),
                meta_diaria_janta=float(meta_diaria_janta),
                dias_trabalhados=dias_trabalhados,
                lucro_desejado=lucro_desejado
            )
            db.add(nova_meta)
            db.commit()
            db.refresh(nova_meta)
            return {"success": True, "message": "Meta salva", "id": nova_meta.id}
            
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao salvar meta: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/planejamento/faturamento-meta/batch")
async def save_faturamento_meta_batch(
    ano: int,
    metas: List[dict],
    db: Session = Depends(get_db)
):
    """Salva metas para múltiplos meses de uma vez"""
    
    for meta in metas:
        existente = db.query(PlanejamentoFaturamento).filter(
            PlanejamentoFaturamento.ano == ano,
            PlanejamentoFaturamento.mes == meta['mes']
        ).first()
        
        if existente:
            existente.meta_diaria_almoco = meta.get('meta_diaria_almoco', 0)
            existente.meta_diaria_janta = meta.get('meta_diaria_janta', 0)
            existente.dias_trabalhados = meta.get('dias_trabalhados', 26)
            existente.lucro_desejado = meta.get('lucro_desejado', 15)
        else:
            nova = PlanejamentoFaturamento(
                ano=ano,
                mes=meta['mes'],
                meta_diaria_almoco=meta.get('meta_diaria_almoco', 0),
                meta_diaria_janta=meta.get('meta_diaria_janta', 0),
                dias_trabalhados=meta.get('dias_trabalhados', 26),
                lucro_desejado=meta.get('lucro_desejado', 15)
            )
            db.add(nova)
    
    db.commit()
    return {"success": True, "message": f"Metas salvas para {len(metas)} meses"}


@router.get("/planejamento/acompanhamento")
async def get_acompanhamento(
    ano: Optional[int] = None,
    mes: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Obtém acompanhamento de faturamento real"""
    if ano is None:
        ano = datetime.now().year
    
    query = db.query(PlanejamentoAcompanhamento).filter(
        PlanejamentoAcompanhamento.ano == ano
    )
    
    if mes:
        query = query.filter(PlanejamentoAcompanhamento.mes == mes)
        resultado = query.first()
        if resultado:
            return {
                "success": True,
                "ano": resultado.ano,
                "mes": resultado.mes,
                "faturamento_almoco": resultado.faturamento_almoco,
                "faturamento_janta": resultado.faturamento_janta,
                "faturamento_total": resultado.faturamento_total,
                "observacao": resultado.observacao
            }
        else:
            return {
                "success": True,
                "ano": ano,
                "mes": mes,
                "faturamento_almoco": 0,
                "faturamento_janta": 0,
                "faturamento_total": 0,
                "observacao": None
            }
    else:
        resultados = query.order_by(PlanejamentoAcompanhamento.mes).all()
        return {
            "success": True,
            "ano": ano,
            "acompanhamentos": [
                {
                    "mes": r.mes,
                    "faturamento_almoco": r.faturamento_almoco,
                    "faturamento_janta": r.faturamento_janta,
                    "faturamento_total": r.faturamento_total,
                    "observacao": r.observacao
                }
                for r in resultados
            ]
        }


@router.post("/planejamento/acompanhamento")
async def save_acompanhamento(
    ano: int,
    mes: int,
    faturamento_almoco: float = 0,
    faturamento_janta: float = 0,
    observacao: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Salva acompanhamento de faturamento real"""
    
    acompanhamento = db.query(PlanejamentoAcompanhamento).filter(
        PlanejamentoAcompanhamento.ano == ano,
        PlanejamentoAcompanhamento.mes == mes
    ).first()
    
    faturamento_total = faturamento_almoco + faturamento_janta
    
    if acompanhamento:
        acompanhamento.faturamento_almoco = faturamento_almoco
        acompanhamento.faturamento_janta = faturamento_janta
        acompanhamento.faturamento_total = faturamento_total
        acompanhamento.observacao = observacao
        db.commit()
        db.refresh(acompanhamento)
        return {"success": True, "message": "Acompanhamento atualizado", "id": acompanhamento.id}
    else:
        novo = PlanejamentoAcompanhamento(
            ano=ano,
            mes=mes,
            faturamento_almoco=faturamento_almoco,
            faturamento_janta=faturamento_janta,
            faturamento_total=faturamento_total,
            observacao=observacao
        )
        db.add(novo)
        db.commit()
        db.refresh(novo)
        return {"success": True, "message": "Acompanhamento salvo", "id": novo.id}


@router.get("/planejamento/acompanhamento/sync-from-lancamentos")
async def sync_acompanhamento_from_lancamentos_get(
    ano: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Versão GET para sincronizar acompanhamento (corrige erro 405)"""
    if ano is None:
        ano = datetime.now().year
    
    try:
        # Buscar lançamentos do ano
        lancamentos = db.query(LivroDiario).filter(
            extract('year', LivroDiario.data) == ano
        ).all()
        
        faturamento_por_mes = {}
        for lanc in lancamentos:
            if lanc.entrada and lanc.entrada > 0:
                mes = lanc.data.month
                faturamento_por_mes[mes] = faturamento_por_mes.get(mes, 0) + float(lanc.entrada)
        
        PCT_ALMOCO = 0.73
        PCT_JANTA = 0.27
        
        resultados = []
        for mes in range(1, 13):
            total = faturamento_por_mes.get(mes, 0)
            almoco = total * PCT_ALMOCO
            janta = total * PCT_JANTA
            
            # Salvar ou atualizar
            existente = db.query(PlanejamentoAcompanhamento).filter(
                PlanejamentoAcompanhamento.ano == ano,
                PlanejamentoAcompanhamento.mes == mes
            ).first()
            
            if existente:
                existente.faturamento_almoco = almoco
                existente.faturamento_janta = janta
                existente.faturamento_total = total
            else:
                novo = PlanejamentoAcompanhamento(
                    ano=ano,
                    mes=mes,
                    faturamento_almoco=almoco,
                    faturamento_janta=janta,
                    faturamento_total=total
                )
                db.add(novo)
            
            resultados.append({
                "mes": mes,
                "faturamento_almoco": almoco,
                "faturamento_janta": janta,
                "faturamento_total": total
            })
        
        db.commit()
        return {
            "success": True, 
            "message": f"Acompanhamento sincronizado para {ano}", 
            "dados": resultados
        }
        
    except Exception as e:
        logger.error(f"Erro ao sincronizar: {e}")
        db.rollback()
        return {"success": False, "message": str(e)}

# ============= ENDPOINTS FICHAS TÉCNICAS =============

class FichaTecnicaCreate(BaseModel):
    nome: str
    categoria: str
    precoVenda: float
    custoTotal: float
    margem: float
    ingredientes: Optional[str] = None
    modoPreparo: Optional[str] = None


class FichaTecnicaResponse(BaseModel):
    id: int
    nome: str
    categoria: str
    preco_venda: float
    custo_total: float
    margem: float
    ingredientes: Optional[str] = None
    modo_preparo: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


@router.get("/fichas-tecnicas", response_model=List[FichaTecnicaResponse])
async def listar_fichas_tecnicas(
    request: Request,
    categoria: Optional[str] = None,
    busca: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Lista todas as fichas técnicas do banco com cache"""
    
    # Cache por 10 minutos
    cache_key = f"fichas_{categoria}_{busca}_{skip}_{limit}"
    
    if cache_key in cache_fichas:
        return cache_fichas[cache_key]
    
    try:
        query = db.query(FichaTecnica)
        
        if categoria:
            query = query.filter(FichaTecnica.categoria == categoria)
        if busca:
            query = query.filter(FichaTecnica.nome.ilike(f"%{busca}%"))
        
        fichas = query.order_by(FichaTecnica.nome).offset(skip).limit(limit).all()
        
        resultado = []
        for f in fichas:
            resultado.append({
                'id': f.id,
                'nome': f.nome,
                'categoria': f.categoria,
                'preco_venda': float(f.preco_venda),
                'custo_total': float(f.custo_total),
                'margem': float(f.margem),
                'ingredientes': f.ingredientes,
                'modo_preparo': f.modo_preparo,
                'created_at': f.created_at,
                'updated_at': f.updated_at
            })
        
        cache_fichas[cache_key] = resultado
        return resultado
        
    except Exception as e:
        logger.error(f"Erro ao listar fichas técnicas: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@router.get("/fichas-tecnicas/{ficha_id}")
async def obter_ficha_tecnica(ficha_id: int, db: Session = Depends(get_db)):
    """Obtém uma ficha técnica específica"""
    try:
        ficha = db.query(FichaTecnica).filter(FichaTecnica.id == ficha_id).first()
        if not ficha:
            raise HTTPException(status_code=404, detail="Ficha não encontrada")
        
        return {
            'id': ficha.id,
            'nome': ficha.nome,
            'categoria': ficha.categoria,
            'preco_venda': float(ficha.preco_venda),
            'custo_total': float(ficha.custo_total),
            'margem': float(ficha.margem),
            'ingredientes': ficha.ingredientes,
            'modo_preparo': ficha.modo_preparo,
            'created_at': ficha.created_at,
            'updated_at': ficha.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao obter ficha {ficha_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fichas-tecnicas")
async def criar_ficha_tecnica(ficha: FichaTecnicaCreate, db: Session = Depends(get_db)):
    """Cria uma nova ficha técnica no banco"""
    try:
        nova_ficha = FichaTecnica(
            nome=ficha.nome,
            categoria=ficha.categoria,
            preco_venda=ficha.precoVenda,
            custo_total=ficha.custoTotal,
            margem=ficha.margem,
            ingredientes=ficha.ingredientes,
            modo_preparo=ficha.modoPreparo
        )
        db.add(nova_ficha)
        db.commit()
        db.refresh(nova_ficha)
        
        # Limpar cache de fichas
        cache_fichas.clear()
        
        return {
            'id': nova_ficha.id,
            'nome': nova_ficha.nome,
            'categoria': nova_ficha.categoria,
            'preco_venda': float(nova_ficha.preco_venda),
            'custo_total': float(nova_ficha.custo_total),
            'margem': float(nova_ficha.margem),
            'ingredientes': nova_ficha.ingredientes,
            'modo_preparo': nova_ficha.modo_preparo,
            'created_at': nova_ficha.created_at,
            'updated_at': nova_ficha.updated_at
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao criar ficha: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/fichas-tecnicas/{ficha_id}")
async def atualizar_ficha_tecnica(ficha_id: int, ficha: FichaTecnicaCreate, db: Session = Depends(get_db)):
    """Atualiza uma ficha técnica existente"""
    try:
        ficha_existente = db.query(FichaTecnica).filter(FichaTecnica.id == ficha_id).first()
        if not ficha_existente:
            raise HTTPException(status_code=404, detail="Ficha não encontrada")
        
        ficha_existente.nome = ficha.nome
        ficha_existente.categoria = ficha.categoria
        ficha_existente.preco_venda = ficha.precoVenda
        ficha_existente.custo_total = ficha.custoTotal
        ficha_existente.margem = ficha.margem
        ficha_existente.ingredientes = ficha.ingredientes
        ficha_existente.modo_preparo = ficha.modoPreparo
        ficha_existente.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(ficha_existente)
        
        # Limpar cache de fichas
        cache_fichas.clear()
        
        return {
            'id': ficha_existente.id,
            'nome': ficha_existente.nome,
            'categoria': ficha_existente.categoria,
            'preco_venda': float(ficha_existente.preco_venda),
            'custo_total': float(ficha_existente.custo_total),
            'margem': float(ficha_existente.margem),
            'ingredientes': ficha_existente.ingredientes,
            'modo_preparo': ficha_existente.modo_preparo,
            'created_at': ficha_existente.created_at,
            'updated_at': ficha_existente.updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao atualizar ficha: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/fichas-tecnicas/{ficha_id}")
async def deletar_ficha_tecnica(ficha_id: int, db: Session = Depends(get_db)):
    """Remove uma ficha técnica"""
    try:
        ficha = db.query(FichaTecnica).filter(FichaTecnica.id == ficha_id).first()
        if not ficha:
            raise HTTPException(status_code=404, detail="Ficha não encontrada")
        
        db.delete(ficha)
        db.commit()
        
        # Limpar cache de fichas
        cache_fichas.clear()
        
        return {"message": "Ficha removida com sucesso"}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Erro ao deletar ficha: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/produtos/teste")
async def test_produtos(db: Session = Depends(get_db)):
    """Endpoint de teste para verificar estrutura dos produtos"""
    try:
        produtos = db.query(Produto).limit(5).all()
        
        resultado = []
        for p in produtos:
            resultado.append({
                'id': p.id,
                'nome': p.descricao,
                'preco_venda': float(p.valor_unitario) if p.valor_unitario else 0,
                'unidade': p.unidade,
                'codigo': p.codigo,
                # Mostrar todos os atributos disponíveis
                'debug_atributos': dir(p)[:20]  # Mostra primeiros 20 atributos
            })
        
        return {
            "success": True,
            "total": len(resultado),
            "produtos": resultado,
            "primeiro_produto_completo": produtos[0].__dict__ if produtos else None
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }