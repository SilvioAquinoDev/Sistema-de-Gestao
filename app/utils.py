import re
import hashlib
import qrcode
from io import BytesIO
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging
import json
import pandas as pd
from decimal import Decimal, InvalidOperation
import requests
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= FUNÇÕES DE VALIDAÇÃO =============

def validar_chave_acesso(chave: str) -> bool:
    """
    Valida se a chave de acesso tem o formato correto (44 dígitos)
    """
    if not chave:
        return False
    
    # Remove caracteres não numéricos
    chave_limpa = re.sub(r'\D', '', chave)
    
    # Verifica se tem 44 dígitos
    if len(chave_limpa) != 44:
        return False
    
    # Verifica se todos são dígitos
    if not chave_limpa.isdigit():
        return False
    
    # Validação do dígito verificador (algoritmo módulo 11)
    return validar_dv_chave_acesso(chave_limpa)

def validar_dv_chave_acesso(chave: str) -> bool:
    """
    Valida o dígito verificador da chave de acesso
    Algoritmo módulo 11 - padrão da NF-e
    """
    if len(chave) != 44:
        return False
    
    # Pega o dígito verificador (posição 43, 44º caractere)
    dv_informado = int(chave[43])
    
    # Calcula o dígito verificador
    peso = 2
    soma = 0
    
    # Percorre os 43 primeiros dígitos
    for i in range(42, -1, -1):
        soma += int(chave[i]) * peso
        peso += 1
        if peso > 9:
            peso = 2
    
    # Calcula o resto da divisão por 11
    resto = soma % 11
    
    # Dígito verificador calculado
    if resto == 0 or resto == 1:
        dv_calculado = 0
    else:
        dv_calculado = 11 - resto
    
    return dv_informado == dv_calculado

def validar_cnpj(cnpj: str) -> bool:
    """
    Valida se o CNPJ é válido
    """
    # Remove caracteres não numéricos
    cnpj = re.sub(r'\D', '', cnpj)
    
    if len(cnpj) != 14:
        return False
    
    # Calcula primeiro dígito verificador
    soma = 0
    peso = 5
    for i in range(12):
        soma += int(cnpj[i]) * peso
        peso -= 1
        if peso < 2:
            peso = 9
    
    resto = soma % 11
    dv1 = 0 if resto < 2 else 11 - resto
    
    # Calcula segundo dígito verificador
    soma = 0
    peso = 6
    for i in range(13):
        soma += int(cnpj[i]) * peso
        peso -= 1
        if peso < 2:
            peso = 9
    
    resto = soma % 11
    dv2 = 0 if resto < 2 else 11 - resto
    
    return dv1 == int(cnpj[12]) and dv2 == int(cnpj[13])

def validar_cpf(cpf: str) -> bool:
    """
    Valida se o CPF é válido
    """
    # Remove caracteres não numéricos
    cpf = re.sub(r'\D', '', cpf)
    
    if len(cpf) != 11:
        return False
    
    # Verifica se todos os dígitos são iguais
    if cpf == cpf[0] * 11:
        return False
    
    # Calcula primeiro dígito verificador
    soma = 0
    for i in range(9):
        soma += int(cpf[i]) * (10 - i)
    
    resto = soma % 11
    dv1 = 0 if resto < 2 else 11 - resto
    
    # Calcula segundo dígito verificador
    soma = 0
    for i in range(10):
        soma += int(cpf[i]) * (11 - i)
    
    resto = soma % 11
    dv2 = 0 if resto < 2 else 11 - resto
    
    return dv1 == int(cpf[9]) and dv2 == int(cpf[10])

def validar_url_nfce(url: str) -> bool:
    """
    Valida se a URL pertence à SEFAZ-PE
    """
    if not url:
        return False
    
    try:
        parsed = urlparse(url)
        # Verifica domínio
        if 'nfce.sefaz.pe.gov.br' not in parsed.netloc:
            return False
        
        # Verifica parâmetros obrigatórios
        params = parse_qs(parsed.query)
        if 'p' not in params or 'u' not in params:
            return False
        
        # Valida chave de acesso
        chave = params['p'][0]
        if not validar_chave_acesso(chave):
            return False
        
        return True
    
    except Exception as e:
        logger.error(f"Erro ao validar URL: {e}")
        return False

# ============= FUNÇÕES DE FORMATAÇÃO =============

def formatar_cnpj(cnpj: str) -> str:
    """
    Formata CNPJ para o padrão XX.XXX.XXX/XXXX-XX
    """
    cnpj = re.sub(r'\D', '', cnpj)
    if len(cnpj) == 14:
        return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    return cnpj

def formatar_cpf(cpf: str) -> str:
    """
    Formata CPF para o padrão XXX.XXX.XXX-XX
    """
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) == 11:
        return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    return cpf

def formatar_telefone(telefone: str) -> str:
    """
    Formata telefone para padrão (XX) XXXX-XXXX ou (XX) XXXXX-XXXX
    """
    telefone = re.sub(r'\D', '', telefone)
    if len(telefone) == 10:
        return f"({telefone[:2]}) {telefone[2:6]}-{telefone[6:]}"
    elif len(telefone) == 11:
        return f"({telefone[:2]}) {telefone[2:7]}-{telefone[7:]}"
    return telefone

def formatar_cep(cep: str) -> str:
    """
    Formata CEP para padrão XXXXX-XXX
    """
    cep = re.sub(r'\D', '', cep)
    if len(cep) == 8:
        return f"{cep[:5]}-{cep[5:]}"
    return cep

def formatar_moeda(valor: float) -> str:
    """
    Formata valor monetário para padrão brasileiro
    """
    try:
        if valor is None:
            return "R$ 0,00"
        return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$ 0,00"

def formatar_porcentagem(valor: float, casas_decimais: int = 2) -> str:
    """
    Formata porcentagem
    """
    return f"{valor:.{casas_decimais}f}%"

def formatar_data_brasil(data: datetime) -> str:
    """
    Formata data para padrão brasileiro (DD/MM/YYYY)
    """
    if not data:
        return ""
    return data.strftime("%d/%m/%Y")

def formatar_hora_brasil(data: datetime) -> str:
    """
    Formata hora para padrão brasileiro (HH:MM:SS)
    """
    if not data:
        return ""
    return data.strftime("%H:%M:%S")

def formatar_data_hora_brasil(data: datetime) -> str:
    """
    Formata data e hora para padrão brasileiro (DD/MM/YYYY HH:MM:SS)
    """
    if not data:
        return ""
    return data.strftime("%d/%m/%Y %H:%M:%S")

# ============= FUNÇÕES DE CONVERSÃO =============

def converter_para_decimal(valor_str: str) -> Decimal:
    """
    Converte string para Decimal tratando formato brasileiro
    """
    if not valor_str:
        return Decimal('0')
    
    try:
        # Remove caracteres não numéricos exceto vírgula e ponto
        valor_str = re.sub(r'[^\d,\-\.]', '', valor_str.strip())
        
        # Trata formato brasileiro (1.234,56)
        if ',' in valor_str and '.' in valor_str:
            # Remove pontos de milhar e troca vírgula por ponto
            valor_str = valor_str.replace('.', '').replace(',', '.')
        elif ',' in valor_str:
            valor_str = valor_str.replace(',', '.')
        
        return Decimal(valor_str)
    
    except InvalidOperation:
        logger.warning(f"Não foi possível converter '{valor_str}' para Decimal")
        return Decimal('0')

def decimal_para_float(valor: Decimal) -> float:
    """
    Converte Decimal para float
    """
    try:
        return float(valor)
    except:
        return 0.0

# ============= FUNÇÕES DE VALIDAÇÃO DE DADOS =============

def sanitizar_html(html: str) -> str:
    """
    Remove possíveis scripts maliciosos do HTML
    """
    if not html:
        return ""
    
    # Remove scripts
    html = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', html, flags=re.IGNORECASE)
    
    # Remove event handlers
    html = re.sub(r'\son\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)
    
    # Remove javascript: protocol
    html = re.sub(r'javascript\s*:', '', html, flags=re.IGNORECASE)
    
    return html

def limpar_texto(texto: str) -> str:
    """
    Remove espaços extras e caracteres especiais do texto
    """
    if not texto:
        return ""
    
    # Remove espaços extras
    texto = re.sub(r'\s+', ' ', texto.strip())
    
    # Remove caracteres de controle
    texto = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', texto)
    
    return texto

# ============= FUNÇÕES DE GERAÇÃO =============

def gerar_hash_chave(chave_acesso: str) -> str:
    """
    Gera um hash MD5 da chave de acesso
    """
    return hashlib.md5(chave_acesso.encode()).hexdigest()

def gerar_qrcode_base64(chave_acesso: str, tamanho: int = 200) -> str:
    """
    Gera QR Code em base64 para exibição no frontend
    """
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(chave_acesso)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Converte para base64
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return f"data:image/png;base64,{img_str}"
    
    except Exception as e:
        logger.error(f"Erro ao gerar QR Code: {e}")
        return ""

# ============= FUNÇÕES DE EXPORTAÇÃO =============

def exportar_para_csv(nota_data: Dict[str, Any]) -> str:
    """
    Exporta dados da nota para CSV
    """
    try:
        # Dados principais
        dados_principais = {
            'Chave Acesso': nota_data.get('chave_acesso'),
            'Número': nota_data.get('numero'),
            'Série': nota_data.get('serie'),
            'Data Emissão': nota_data.get('data_emissao'),
            'CNPJ': nota_data.get('cnpj_emitente'),
            'Nome Emitente': nota_data.get('nome_emitente'),
            'Valor Total': nota_data.get('valor_total')
        }
        
        # Produtos
        produtos_df = pd.DataFrame(nota_data.get('produtos', []))
        
        # Pagamentos
        pagamentos_df = pd.DataFrame(nota_data.get('pagamentos', []))
        
        # Salva em CSV
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pd.DataFrame([dados_principais]).to_excel(writer, sheet_name='Dados Principais', index=False)
            produtos_df.to_excel(writer, sheet_name='Produtos', index=False)
            pagamentos_df.to_excel(writer, sheet_name='Pagamentos', index=False)
        
        output.seek(0)
        return base64.b64encode(output.getvalue()).decode()
    
    except Exception as e:
        logger.error(f"Erro ao exportar para Excel: {e}")
        return ""

# ============= FUNÇÕES DE LOGGING =============

def log_request_info(request, process_time: float = 0):
    """
    Registra informações da requisição
    """
    logger.info({
        'method': request.method,
        'url': str(request.url),
        'client': request.client.host if request.client else 'unknown',
        'process_time': f"{process_time:.3f}s"
    })

def log_error(error: Exception, context: str = ""):
    """
    Registra erros com contexto
    """
    logger.error(f"Erro em {context}: {str(error)}", exc_info=True)

# ============= FUNÇÕES DE MONITORAMENTO =============

def calcular_tempo_processamento(inicio: datetime, fim: datetime) -> float:
    """
    Calcula tempo de processamento em segundos
    """
    delta = fim - inicio
    return delta.total_seconds()

def verificar_health_check() -> Dict[str, Any]:
    """
    Verifica status do sistema
    """
    return {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0'
    }

# ============= FUNÇÕES DE CACHE =============

class SimpleCache:
    """
    Cache simples em memória para dados
    """
    def __init__(self, ttl_seconds: int = 300):
        self.cache = {}
        self.ttl = ttl_seconds
    
    def get(self, key: str):
        """Recupera item do cache"""
        if key in self.cache:
            data, timestamp = self.cache[key]
            if datetime.now() - timestamp < timedelta(seconds=self.ttl):
                return data
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Any):
        """Adiciona item ao cache"""
        self.cache[key] = (value, datetime.now())
    
    def clear(self):
        """Limpa o cache"""
        self.cache.clear()
    
    def remove(self, key: str):
        """Remove item específico do cache"""
        if key in self.cache:
            del self.cache[key]

# ============= FUNÇÕES DE MASCARAMENTO =============

def mascarar_cnpj(cnpj: str) -> str:
    """
    Mascara CNPJ para exibição parcial (mostra apenas últimos 4 dígitos)
    """
    cnpj = re.sub(r'\D', '', cnpj)
    if len(cnpj) == 14:
        return f"***{cnpj[-4:]}"
    return cnpj

def mascarar_cpf(cpf: str) -> str:
    """
    Mascara CPF para exibição parcial (mostra apenas últimos 4 dígitos)
    """
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) == 11:
        return f"***.{cpf[-4:]}"
    return cpf

def mascarar_chave_acesso(chave: str) -> str:
    """
    Mascara chave de acesso (mostra primeiros 8 e últimos 8 dígitos)
    """
    chave = re.sub(r'\D', '', chave)
    if len(chave) == 44:
        return f"{chave[:8]}...{chave[-8:]}"
    return chave

# ============= FUNÇÕES DE VALIDAÇÃO ADICIONAIS =============

def validar_email(email: str) -> bool:
    """
    Valida formato de email
    """
    padrao = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(padrao, email))

def validar_telefone(telefone: str) -> bool:
    """
    Valida formato de telefone brasileiro
    """
    telefone = re.sub(r'\D', '', telefone)
    return len(telefone) in [10, 11]

def validar_cep(cep: str) -> bool:
    """
    Valida formato de CEP brasileiro
    """
    cep = re.sub(r'\D', '', cep)
    return len(cep) == 8

# ============= FUNÇÕES DE EXTRAÇÃO =============

def extrair_numeros(texto: str) -> str:
    """
    Extrai apenas números de um texto
    """
    return re.sub(r'\D', '', texto)

def extrair_valores_monetarios(texto: str) -> list:
    """
    Extrai todos os valores monetários de um texto
    """
    padrao = r'R?\$?\s*([\d\.]+,\d{2})'
    matches = re.findall(padrao, texto)
    return [converter_para_decimal(match) for match in matches]

# ============= FUNÇÕES DE COMPARAÇÃO =============

def comparar_valores(valor1: float, valor2: float, tolerancia: float = 0.01) -> bool:
    """
    Compara dois valores com tolerância para erros de arredondamento
    """
    return abs(valor1 - valor2) <= tolerancia

def somar_produtos(produtos: list) -> float:
    """
    Soma o valor total de todos os produtos
    """
    total = sum(p.get('valor_total', 0) for p in produtos)
    return total

# Exporta funções principais
__all__ = [
    'validar_chave_acesso',
    'validar_cnpj',
    'validar_cpf',
    'validar_url_nfce',
    'formatar_cnpj',
    'formatar_cpf',
    'formatar_moeda',
    'formatar_data_brasil',
    'formatar_data_hora_brasil',
    'converter_para_decimal',
    'gerar_qrcode_base64',
    'limpar_texto',
    'sanitizar_html',
    'mascarar_chave_acesso',
    'SimpleCache',
    'extrair_numeros'
]