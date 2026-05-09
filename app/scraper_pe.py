# app/scraper_pe.py - VERSÃO ATUALIZADA PARA AMBOS OS FORMATOS

import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging
from urllib.parse import urlparse, parse_qs, unquote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class NFCeScraperPE:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
    
    def validar_url(self, url: str) -> bool:
        """Valida se a URL é da SEFAZ-PE"""
        return 'nfce.sefaz.pe.gov.br' in url.lower()
    
    def extrair_parametros_url(self, url: str) -> Dict:
        """Extrai e decodifica os parâmetros da URL"""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        resultado = {}
        if 'p' in params:
            p_value = params['p'][0]
            # Decodifica URL encoding se necessário
            p_value = unquote(p_value)
            
            # Verifica se é o formato com pipes
            if '|' in p_value:
                partes = p_value.split('|')
                if len(partes) >= 5:
                    resultado = {
                        'chave': partes[0],
                        'serie': partes[1],
                        'numero': partes[2],
                        'tipo': partes[3],
                        'hash': partes[4] if len(partes) > 4 else ''
                    }
                    logger.info(f"URL no formato pipe: chave={resultado['chave'][:10]}...")
            else:
                # Formato antigo (apenas chave)
                resultado = {'chave': p_value}
                logger.info(f"URL no formato chave única")
        
        return resultado
    
    def baixar_pagina(self, url: str, max_tentativas: int = 3) -> str:
        """Baixa o conteúdo da página com retry"""
        for tentativa in range(max_tentativas):
            try:
                logger.info(f"Tentativa {tentativa + 1} de baixar a URL")
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                
                # Verificar se é nota cancelada
                if "Cancelada" in response.text or "nota cancelada" in response.text.lower():
                    raise Exception("Nota Fiscal cancelada/inutilizada")
                
                # Salvar HTML para debug
                if tentativa == 0:
                    with open('debug_nota.html', 'w', encoding='utf-8') as f:
                        f.write(response.text)
                    logger.info("HTML salvo em debug_nota.html")
                
                logger.info(f"Página baixada: {len(response.text)} bytes")
                return response.text
                
            except requests.RequestException as e:
                logger.warning(f"Tentativa {tentativa + 1} falhou: {e}")
                if tentativa == max_tentativas - 1:
                    raise Exception(f"Falha ao baixar página: {e}")
        return ""
    
    def extrair_chave_acesso(self, soup: BeautifulSoup, url_params: Dict = None) -> str:
        """Extrai chave de acesso de 44 dígitos"""
        # Primeiro, tenta obter dos parâmetros da URL
        if url_params and 'chave' in url_params:
            chave = url_params['chave']
            if len(chave) == 44 and chave.isdigit():
                logger.info(f"Chave extraída da URL: {chave[:10]}...")
                return chave
        
        # Se não, busca no HTML
        texto = soup.get_text()
        padroes = [
            r'\b\d{44}\b',
            r'Chave de Acesso[:\s]*(\d{44})',
            r'NFC-e[:\s]*(\d{44})',
            r'Chave[:\s]*(\d{44})'
        ]
        
        for padrao in padroes:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                chave = match.group(1) if len(match.groups()) > 0 else match.group(0)
                logger.info(f"Chave encontrada no HTML: {chave[:10]}...")
                return chave
        
        raise Exception("Chave de acesso não encontrada")
    
    def extrair_numero_serie(self, soup: BeautifulSoup, url_params: Dict = None) -> Tuple[int, int]:
        """Extrai número e série da nota"""
        numero = 0
        serie = 1
        
        # Tenta obter dos parâmetros da URL
        if url_params:
            if 'numero' in url_params:
                try:
                    numero = int(url_params['numero'])
                    logger.info(f"Número da URL: {numero}")
                except:
                    pass
            if 'serie' in url_params:
                try:
                    serie = int(url_params['serie'])
                    logger.info(f"Série da URL: {serie}")
                except:
                    pass
        
        # Se não encontrou, busca no HTML
        if numero == 0:
            texto = soup.get_text()
            padroes_numero = [
                r'N[°º]\s*N[úu]mero[:\s]*(\d+)',
                r'Número[:\s]*(\d+)',
                r'NOTA\s*FISCAL\s*N°\s*(\d+)'
            ]
            for padrao in padroes_numero:
                match = re.search(padrao, texto, re.IGNORECASE)
                if match:
                    numero = int(match.group(1))
                    break
        
        if serie == 1:
            texto = soup.get_text()
            padroes_serie = [
                r'S[ée]rie[:\s]*(\d+)',
                r'Série[:\s]*(\d+)'
            ]
            for padrao in padroes_serie:
                match = re.search(padrao, texto, re.IGNORECASE)
                if match:
                    serie = int(match.group(1))
                    break
        
        return numero, serie
    
    def extrair_data_hora(self, soup: BeautifulSoup) -> datetime:
        """Extrai data e hora da emissão"""
        texto = soup.get_text()
        
        padroes = [
            r'Data[:\s]*(\d{2}/\d{2}/\d{4})\s*Hora[:\s]*(\d{2}:\d{2}:\d{2})',
            r'Emissão[:\s]*(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2}:\d{2})',
            r'(\d{2}/\d{2}/\d{4})\s*(\d{2}:\d{2}:\d{2})',
            r'(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2}:\d{2})'
        ]
        
        for padrao in padroes:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                data_str = match.group(1)
                hora_str = match.group(2) if len(match.groups()) > 1 else "00:00:00"
                
                # Tenta diferentes formatos de data
                for formato in ['%d/%m/%Y', '%Y-%m-%d']:
                    try:
                        data = datetime.strptime(data_str, formato)
                        hora = datetime.strptime(hora_str, '%H:%M:%S').time()
                        return datetime.combine(data.date(), hora)
                    except:
                        continue
        
        logger.warning("Data não encontrada, usando data atual")
        return datetime.now()
    
    def extrair_emitente(self, soup: BeautifulSoup) -> Tuple[str, str, str, str]:
        """Extrai dados do emitente"""
        texto = soup.get_text()
        
        # CNPJ
        cnpj = ""
        padroes_cnpj = [
            r'CNPJ[:\s]*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})',
            r'CNPJ[:\s]*(\d{14})'
        ]
        for padrao in padroes_cnpj:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                cnpj = match.group(1)
                break
        
        # IE
        ie = ""
        padroes_ie = [
            r'Inscrição Estadual[:\s]*([\d\.\-]+)',
            r'I\.E\.[:\s]*([\d\.\-]+)'
        ]
        for padrao in padroes_ie:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                ie = match.group(1)
                break
        
        # Nome
        nome = ""
        padroes_nome = [
            r'RAZÃO SOCIAL[:\s]*([^\n]+)',
            r'NOME[:\s]*([^\n]+)',
            r'EMITENTE[:\s]*([^\n]+)'
        ]
        for padrao in padroes_nome:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                nome = match.group(1).strip()
                if len(nome) > 3:
                    break
        
        # Endereço
        endereco = ""
        padroes_end = [
            r'ENDEREÇO[:\s]*([^\n]+)',
            r'Endereço[:\s]*([^\n]+)'
        ]
        for padrao in padroes_end:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                endereco = match.group(1).strip()
                break
        
        return cnpj, nome, ie, endereco
    
    def extrair_produtos(self, soup: BeautifulSoup) -> List[Dict]:
        """Extrai produtos - versão robusta"""
        produtos = []
        
        # Busca por tabelas
        tables = soup.find_all('table')
        logger.info(f"Encontradas {len(tables)} tabelas")
        
        for table in tables:
            rows = table.find_all('tr')
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    # Tenta identificar cada coluna
                    produto = {}
                    
                    for i, col in enumerate(cols):
                        text = col.get_text(strip=True)
                        if i == 0 and text:
                            produto['codigo'] = text
                        elif i == 1 and text:
                            produto['descricao'] = text
                        elif text:
                            # Tenta identificar quantidade, valor unitário, total
                            if re.search(r'^\d+[,.]?\d*$', text):
                                if 'quantidade' not in produto:
                                    produto['quantidade'] = self._parse_decimal(text)
                            elif re.search(r'R?\$?\s*\d+[,.]\d{2}', text):
                                if 'valor_unitario' not in produto:
                                    produto['valor_unitario'] = self._parse_decimal(text)
                                elif 'valor_total' not in produto:
                                    produto['valor_total'] = self._parse_decimal(text)
                    
                    if 'descricao' in produto and produto['descricao']:
                        # Define valores padrão
                        produto.setdefault('codigo', '')
                        produto.setdefault('unidade', self._extrair_unidade(produto['descricao']))
                        produto.setdefault('quantidade', 1)
                        produto.setdefault('valor_unitario', 0)
                        produto.setdefault('valor_total', 0)
                        
                        produtos.append(produto)
        
        logger.info(f"Extraídos {len(produtos)} produtos")
        return produtos
    
    def extrair_valor_total(self, soup: BeautifulSoup) -> float:
        """Extrai valor total"""
        texto = soup.get_text()
        
        padroes = [
            r'VALOR TOTAL[:\s]*R?\$?\s*([\d\.,]+)',
            r'Total[:\s]*R?\$?\s*([\d\.,]+)',
            r'Total a Pagar[:\s]*R?\$?\s*([\d\.,]+)'
        ]
        
        for padrao in padroes:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                return self._parse_decimal(match.group(1))
        
        return 0.0
    
    def extrair_pagamentos(self, soup: BeautifulSoup) -> List[Dict]:
        """Extrai formas de pagamento"""
        pagamentos = []
        texto = soup.get_text()
        
        formas = {
            'Dinheiro': r'Dinheiro[:\s]*R?\$?\s*([\d\.,]+)',
            'Cartão de Crédito': r'Cartão de Crédito[:\s]*R?\$?\s*([\d\.,]+)',
            'Cartão de Débito': r'Cartão de Débito[:\s]*R?\$?\s*([\d\.,]+)',
            'PIX': r'PIX[:\s]*R?\$?\s*([\d\.,]+)'
        }
        
        for forma, padrao in formas.items():
            match = re.search(padrao, texto, re.IGNORECASE)
            if match:
                valor = self._parse_decimal(match.group(1))
                if valor > 0:
                    pagamentos.append({'forma_pagamento': forma, 'valor': valor})
        
        if not pagamentos:
            total = self.extrair_valor_total(soup)
            if total > 0:
                pagamentos.append({'forma_pagamento': 'Não especificado', 'valor': total})
        
        return pagamentos
    
    def extrair_cpf_consumidor(self, soup: BeautifulSoup) -> Optional[str]:
        """Extrai CPF do consumidor"""
        texto = soup.get_text()
        match = re.search(r'CPF[:\s]*(\d{3}\.\d{3}\.\d{3}-\d{2})', texto, re.IGNORECASE)
        return match.group(1) if match else None
    
    def _parse_decimal(self, valor_str: str) -> float:
        """Converte string para float"""
        if not valor_str:
            return 0.0
        try:
            valor_str = valor_str.replace('R$', '').replace('$', '').strip()
            valor_str = valor_str.replace('.', '').replace(',', '.')
            valor_str = re.sub(r'[^\d.-]', '', valor_str)
            return float(valor_str) if valor_str else 0.0
        except:
            return 0.0
    
    def _extrair_unidade(self, descricao: str) -> str:
        """Extrai unidade da descrição"""
        unidades = {'UN': r'\b(UN|UNIDADE)\b', 'KG': r'\b(KG|QUILO)\b', 'L': r'\b(L|LITRO)\b'}
        for unidade, padrao in unidades.items():
            if re.search(padrao, descricao, re.IGNORECASE):
                return unidade
        return "UN"
    
    def processar_nota(self, url: str) -> Dict:
        """Processa a nota fiscal"""
        # Extrai parâmetros da URL
        url_params = self.extrair_parametros_url(url)
        
        # Baixa página
        html = self.baixar_pagina(url)
        soup = BeautifulSoup(html, 'html.parser')
        
        # Extrai dados
        chave_acesso = self.extrair_chave_acesso(soup, url_params)
        numero, serie = self.extrair_numero_serie(soup, url_params)
        data_emissao = self.extrair_data_hora(soup)
        cnpj, nome, ie, endereco = self.extrair_emitente(soup)
        produtos = self.extrair_produtos(soup)
        valor_total = self.extrair_valor_total(soup)
        
        if valor_total == 0 and produtos:
            valor_total = sum(p.get('valor_total', 0) for p in produtos)
        
        pagamentos = self.extrair_pagamentos(soup)
        cpf_consumidor = self.extrair_cpf_consumidor(soup)
        
        if not produtos:
            raise Exception("Não foi possível extrair os produtos")
        
        return {
            'chave_acesso': chave_acesso,
            'numero': numero,
            'serie': serie,
            'data_emissao': data_emissao,
            'cnpj_emitente': cnpj,
            'nome_emitente': nome,
            'ie_emitente': ie,
            'endereco_emitente': endereco,
            'valor_total': valor_total,
            'cpf_consumidor': cpf_consumidor,
            'produtos': produtos,
            'pagamentos': pagamentos
        }