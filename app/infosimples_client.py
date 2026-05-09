# app/infosimples_client.py - Versão que processa XML
import requests
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InfosimplesClient:
    """Cliente para consulta NFC-e via XML da SEFAZ-PE"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/xml, text/xml, */*',
        })

    def validar_url(self, url: str) -> bool:
        """Valida se a URL é da SEFAZ-PE"""
        return 'nfce.sefaz.pe.gov.br' in url.lower()
    
    def extrair_chave_da_url(self, url: str) -> str:
        """Extrai a chave de acesso de 44 dígitos da URL"""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        if 'p' in params:
            p_value = unquote(params['p'][0])
            # Pega os primeiros 44 dígitos
            chave = re.sub(r'\D', '', p_value)[:44]
            if len(chave) == 44:
                logger.info(f"Chave extraída: {chave}")
                return chave
        
        raise Exception("Não foi possível extrair a chave de acesso")

    def consultar_nfce(self, url: str) -> Dict:
        """Consulta NFC-e e processa o XML retornado"""
        
        logger.info(f"Baixando XML da nota: {url}")
        
        try:
            # Baixa o XML da SEFAZ
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            content = response.text
            logger.info(f"XML baixado: {len(content)} bytes")
            
            # Salva para debug
            with open('nota_xml.xml', 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Processa o XML
            dados = self._processar_xml(content, url)
            
            return dados
            
        except requests.exceptions.Timeout:
            raise Exception("Timeout ao acessar a SEFAZ-PE")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Erro ao acessar SEFAZ-PE: {e}")
    
    def _processar_xml(self, xml_content: str, url_original: str) -> Dict:
        """Processa o XML da NFC-e e extrai os dados"""
        
        # Remove namespaces para facilitar a busca
        xml_content = re.sub(r'xmlns="[^"]+"', '', xml_content)
        xml_content = re.sub(r'xmlns:[^=]+="[^"]+"', '', xml_content)
        
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error(f"Erro ao parsear XML: {e}")
            raise Exception("Erro ao processar o XML da nota fiscal")
        
        # Busca pelo elemento NFe
        nfe = root.find('.//NFe')
        if nfe is None:
            nfe = root.find('.//infNFe')
            if nfe is None:
                raise Exception("Estrutura XML não reconhecida")
        
        infNFe = nfe.find('.//infNFe')
        if infNFe is None:
            infNFe = nfe
        
        # Extrai chave de acesso
        chave_acesso = self.extrair_chave_da_url(url_original)
        
        # 1. Dados do emitente
        emit = infNFe.find('.//emit')
        cnpj = ""
        nome_emitente = ""
        if emit is not None:
            cnpj_elem = emit.find('.//CNPJ')
            if cnpj_elem is not None:
                cnpj = cnpj_elem.text
                # Formata CNPJ
                if len(cnpj) == 14:
                    cnpj = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
            
            nome_elem = emit.find('.//xNome')
            if nome_elem is not None:
                nome_emitente = nome_elem.text
        
        # 2. Data de emissão
        ide = infNFe.find('.//ide')
        data_emissao = datetime.now()
        if ide is not None:
            dhEmi = ide.find('.//dhEmi')
            if dhEmi is not None and dhEmi.text:
                try:
                    # Formato: 2026-04-30T09:52:25-03:00
                    data_emissao = datetime.fromisoformat(dhEmi.text.replace('-03:00', ''))
                except:
                    pass
        
        # 3. Número da nota
        numero = 0
        if ide is not None:
            nNF = ide.find('.//nNF')
            if nNF is not None and nNF.text:
                try:
                    numero = int(nNF.text)
                except:
                    pass
        
        # 4. Produtos
        produtos = []
        dets = infNFe.findall('.//det')
        for det in dets:
            prod = det.find('.//prod')
            if prod is not None:
                produto = {
                    'codigo': '',
                    'descricao': '',
                    'unidade': 'UN',
                    'quantidade': 1.0,
                    'valor_unitario': 0.0,
                    'valor_total': 0.0
                }
                
                # Código
                cProd = prod.find('.//cProd')
                if cProd is not None:
                    produto['codigo'] = cProd.text or ''
                
                # Descrição
                xProd = prod.find('.//xProd')
                if xProd is not None:
                    produto['descricao'] = xProd.text or ''
                
                # Unidade
                uCom = prod.find('.//uCom')
                if uCom is not None:
                    produto['unidade'] = uCom.text or 'UN'
                
                # Quantidade
                qCom = prod.find('.//qCom')
                if qCom is not None and qCom.text:
                    try:
                        produto['quantidade'] = float(qCom.text.replace(',', '.'))
                    except:
                        pass
                
                # Valor unitário
                vUnCom = prod.find('.//vUnCom')
                if vUnCom is not None and vUnCom.text:
                    try:
                        produto['valor_unitario'] = float(vUnCom.text.replace(',', '.'))
                    except:
                        pass
                
                # Valor total
                vProd = prod.find('.//vProd')
                if vProd is not None and vProd.text:
                    try:
                        produto['valor_total'] = float(vProd.text.replace(',', '.'))
                    except:
                        pass
                
                # Se não tem valor total mas tem unitário e quantidade
                if produto['valor_total'] == 0 and produto['valor_unitario'] > 0:
                    produto['valor_total'] = produto['quantidade'] * produto['valor_unitario']
                
                if produto['descricao']:
                    produtos.append(produto)
        
        # 5. Valor total da nota
        total = infNFe.find('.//total')
        valor_total = 0.0
        if total is not None:
            ICMSTot = total.find('.//ICMSTot')
            if ICMSTot is not None:
                vNF = ICMSTot.find('.//vNF')
                if vNF is not None and vNF.text:
                    try:
                        valor_total = float(vNF.text.replace(',', '.'))
                    except:
                        pass
        
        # Se não encontrou no total, tenta no vNFTot
        if valor_total == 0:
            vNFTot = infNFe.find('.//vNFTot')
            if vNFTot is not None and vNFTot.text:
                try:
                    valor_total = float(vNFTot.text.replace(',', '.'))
                except:
                    pass
        
        # 6. Pagamentos
        pagamentos = []
        pag = infNFe.find('.//pag')
        if pag is not None:
            detPag = pag.findall('.//detPag')
            for dp in detPag:
                tPag = dp.find('.//tPag')
                vPag = dp.find('.//vPag')
                
                forma = "Não especificado"
                if tPag is not None:
                    tipo = tPag.text
                    formas = {
                        '01': 'Dinheiro',
                        '02': 'Cheque',
                        '03': 'Cartão de Crédito',
                        '04': 'Cartão de Débito',
                        '05': 'Crédito Loja',
                        '10': 'Vale Alimentação',
                        '11': 'Vale Refeição',
                        '12': 'Vale Presente',
                        '13': 'Vale Combustível',
                        '14': 'Duplicata Mercantil',
                        '15': 'Boleto Bancário',
                        '16': 'Depósito Bancário',
                        '17': 'Pagamento Instantâneo (PIX)',
                        '18': 'Transferência bancária, Carteira Digital',
                        '19': 'Programa de fidelidade, Cashback, Crédito Virtual',
                        '90': 'Sem pagamento',
                        '99': 'Outros'
                    }
                    forma = formas.get(tipo, f'Tipo {tipo}')
                
                valor = 0.0
                if vPag is not None and vPag.text:
                    try:
                        valor = float(vPag.text.replace(',', '.'))
                    except:
                        pass
                
                if valor > 0:
                    pagamentos.append({
                        'forma_pagamento': forma,
                        'valor': valor
                    })
        
        # Se não encontrou pagamentos, usa o valor total
        if not pagamentos and valor_total > 0:
            pagamentos.append({
                'forma_pagamento': 'Não especificado',
                'valor': valor_total
            })
        
        # 7. Informações do consumidor
        cpf_consumidor = None
        cobr = infNFe.find('.//cobr')
        if cobr is not None:
            fat = cobr.find('.//fat')
            if fat is not None:
                cpf_elem = fat.find('.//CPF')
                if cpf_elem is not None:
                    cpf_consumidor = cpf_elem.text
        
        # Verifica se nota está cancelada
        protNFe = root.find('.//protNFe')
        is_cancelada = False
        if protNFe is not None:
            infProt = protNFe.find('.//infProt')
            if infProt is not None:
                cStat = infProt.find('.//cStat')
                if cStat is not None and cStat.text == '101':
                    is_cancelada = True
        
        logger.info(f"Dados extraídos: {len(produtos)} produtos, Total: R$ {valor_total}")
        
        return {
            'chave_acesso': chave_acesso,
            'numero': numero,
            'serie': 102,  # Da tag serie no XML
            'data_emissao': data_emissao,
            'cnpj_emitente': cnpj,
            'nome_emitente': nome_emitente,
            'ie_emitente': '',
            'endereco_emitente': '',
            'valor_total': valor_total,
            'cpf_consumidor': cpf_consumidor,
            'produtos': produtos,
            'pagamentos': pagamentos,
            'url_original': url_original,
            'is_cancelada': is_cancelada
        }