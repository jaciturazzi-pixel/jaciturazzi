import pdfplumber
import re
from typing import Dict, List, Optional
from abc import ABC, abstractmethod

def parse_pt_float(val_str: str) -> float:
    if not val_str:
        return 0.0
    return float(val_str.replace('.', '').replace(',', '.'))

class BaseInvoiceParser(ABC):
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.text = self._extract_text()
        self.lines = [line.strip() for line in self.text.split('\n') if line.strip()]
        
    def _extract_text(self) -> str:
        full_text = []
        with pdfplumber.open(self.pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text.append(text)
        return "\n".join(full_text)
        
    @abstractmethod
    def parse_metadata(self) -> Dict:
        pass
        
    @abstractmethod
    def parse_line_items(self) -> List[Dict]:
        pass
        
    def parse(self) -> Dict:
        metadata = self.parse_metadata()
        items = self.parse_line_items()
        metadata['items'] = items
        metadata['pdf_path'] = self.pdf_path
        return metadata

class ContinenteParser(BaseInvoiceParser):
    def parse_metadata(self) -> Dict:
        metadata = {
            "store": "Continente",
            "invoice_number": "",
            "invoice_date": "",
            "nif": "",
            "total_amount": 0.0,
            "discount_amount": 0.0
        }
        
        for line in self.lines:
            # Ex: Nro:FS CYE201/068575 07/09/2026 12:06 | NIF:PT333075544
            match_nro = re.search(r'Nro:(FS\s+\S+)\s+(\d{2}/\d{2}/\d{4})', line)
            if match_nro:
                metadata["invoice_number"] = match_nro.group(1).strip()
                # Converter 07/09/2026 para 2026-09-07
                date_parts = match_nro.group(2).split('/')
                metadata["invoice_date"] = f"{date_parts[2]}-{date_parts[1]}-{date_parts[0]}"
                
            match_nif = re.search(r'NIF:PT(\d+)', line)
            if match_nif:
                metadata["nif"] = match_nif.group(1)
                
            match_total = re.search(r'TOTAL A PAGAR\s+([\d,]+)', line)
            if match_total:
                metadata["total_amount"] = parse_pt_float(match_total.group(1))
                
            match_disc = re.search(r'Total de descontos e poupancas\s+([\d,]+)', line)
            if match_disc:
                metadata["discount_amount"] = parse_pt_float(match_disc.group(1))
                
        return metadata
        
    def parse_line_items(self) -> List[Dict]:
        items = []
        current_item = None
        
        # Regexes
        # Ex: (C) BOL CHOC/CAC S/AC CONT EQUIL SA 2,49
        item_one_line_re = re.compile(r'^\([A-Z]\)\s+(.*?)\s+([\d,]+)$')
        # Ex: (A) MANGA
        item_multiline_re = re.compile(r'^\([A-Z]\)\s+(.*)$')
        # Ex: 1,440 X 2,49 3,59
        qty_price_re = re.compile(r'^([\d,]+)\s*[xX]\s*([\d,]+)\s+([\d,]+)$')
        # Ex: DESCONTO DIRETO 0,57 ou POUPANCA 1,20
        discount_re = re.compile(r'^(?:DESCONTO DIRETO|POUPANCA)\s+([\d,]+)$', re.IGNORECASE)
        
        for line in self.lines:
            # Check Stop Condition (Continente)
            if "SUBTOTAL" in line or "%IVA" in line or "Total Liq." in line or line.startswith("Total de descontos") or "Continente Pay" in line:
                if current_item:
                    items.append(current_item)
                    current_item = None
                break

            # Check if it's a new item (One line format)
            m1 = item_one_line_re.match(line)
            if m1:
                if current_item:
                    items.append(current_item)
                current_item = {
                    "description": m1.group(1).strip(),
                    "quantity": 1.0,
                    "unit": "UN",
                    "unit_price": parse_pt_float(m1.group(2)),
                    "total_price": parse_pt_float(m1.group(2)),
                    "discount": 0.0
                }
                continue
                
            # Check if it's a new item (Multi-line format - just the name)
            m2 = item_multiline_re.match(line)
            if m2 and not m1:
                if current_item:
                    items.append(current_item)
                current_item = {
                    "description": m2.group(1).strip(),
                    "quantity": 1.0,
                    "unit": "UN",
                    "unit_price": 0.0,
                    "total_price": 0.0,
                    "discount": 0.0
                }
                continue
                
            if current_item:
                m_qty = qty_price_re.match(line)
                if m_qty:
                    current_item["quantity"] = parse_pt_float(m_qty.group(1))
                    current_item["unit_price"] = parse_pt_float(m_qty.group(2))
                    current_item["total_price"] = parse_pt_float(m_qty.group(3))
                    if current_item["quantity"] != int(current_item["quantity"]):
                        current_item["unit"] = "KG"
                    continue
                    
                m_disc = discount_re.match(line)
                if m_disc:
                    current_item["discount"] += parse_pt_float(m_disc.group(1))
                    continue
                    
                # Se for "SUBTOTAL", paramos de procurar itens
                if line.startswith("SUBTOTAL"):
                    if current_item:
                        items.append(current_item)
                        current_item = None
                    break

        if current_item:
            items.append(current_item)
            
        return items

class PingoDoceParser(BaseInvoiceParser):
    def parse_metadata(self) -> Dict:
        metadata = {
            "store": "Pingo Doce",
            "invoice_number": "",
            "invoice_date": "",
            "nif": "",
            "total_amount": 0.0,
            "discount_amount": 0.0
        }
        
        for line in self.lines:
            match_nro = re.search(r'Fatura Simplificada\s+FS\s+(\S+)', line)
            if match_nro:
                metadata["invoice_number"] = "FS " + match_nro.group(1)
                
            match_date = re.search(r'Data de emissão:(\d{2}/\d{2}/\d{4})', line)
            if match_date:
                date_parts = match_date.group(1).split('/')
                metadata["invoice_date"] = f"{date_parts[2]}-{date_parts[1]}-{date_parts[0]}"
                
            match_nif = re.search(r'N\. Contribuinte:\s+(\d+)', line)
            if match_nif:
                metadata["nif"] = match_nif.group(1)
                
            match_total = re.search(r'TOTAL A PAGAR\s+([\d,]+)', line)
            if match_total:
                metadata["total_amount"] = parse_pt_float(match_total.group(1))
                
            match_disc = re.search(r'TOTAL POUPANÇA\s+\(([\d,]+)\)', line)
            if match_disc:
                metadata["discount_amount"] = parse_pt_float(match_disc.group(1))
                
        return metadata
        
    def parse_line_items(self) -> List[Dict]:
        items = []
        current_item = None
        
        # Regexes Pingo Doce
        # Ex: C FRANGO PEITO VC 1,034 X 6,49 6,71
        # Ex: C PÃO GIR AB OROW550G 2,99
        item_re = re.compile(r'^[A-Z]\s+(.*?)(?:\s+([\d,]+)\s*[xX]\s*([\d,]+))?\s+([\d,]+)$')
        discount_re = re.compile(r'^Poupança Imediata\s*\(([\d,]+)\)$', re.IGNORECASE)
        
        for line in self.lines:
            # Pingo Doce termina a lista de artigos antes do Resumo
            if line.startswith("Resumo") or "TOTAL A PAGAR" in line or "Resumo IVA" in line or line.startswith("TOTAL POUPANÇA"):
                if current_item:
                    items.append(current_item)
                    current_item = None
                break
                
            if line.startswith("DEPÓSITO VOLTA") or line.startswith("----------------"):
                continue
                    
            m_item = item_re.match(line)
            if m_item:
                if current_item:
                    items.append(current_item)
                    
                desc = m_item.group(1).strip()
                qty_str = m_item.group(2)
                uprice_str = m_item.group(3)
                total_str = m_item.group(4)
                
                qty = parse_pt_float(qty_str) if qty_str else 1.0
                uprice = parse_pt_float(uprice_str) if uprice_str else parse_pt_float(total_str)
                total = parse_pt_float(total_str)
                
                unit = "UN"
                if qty != int(qty):
                    unit = "KG"
                    
                current_item = {
                    "description": desc,
                    "quantity": qty,
                    "unit": unit,
                    "unit_price": uprice,
                    "total_price": total,
                    "discount": 0.0
                }
                continue
                
            if current_item:
                m_disc = discount_re.match(line)
                if m_disc:
                    current_item["discount"] += parse_pt_float(m_disc.group(1))
                    continue

        if current_item:
            items.append(current_item)
            
        return items

class ParserFactory:
    @staticmethod
    def get_parser(pdf_path: str) -> BaseInvoiceParser:
        # Lê apenas a primeira página para identificar a loja
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                raise ValueError(f"O PDF {pdf_path} está vazio.")
            text = pdf.pages[0].extract_text() or ""
            
            text_lower = text.lower()
            if "continente" in text_lower or "modelo" in text_lower:
                return ContinenteParser(pdf_path)
            elif "pingo doce" in text_lower or "jeronimo martins" in text_lower:
                return PingoDoceParser(pdf_path)
            else:
                print(f"Aviso: Loja não identificada automaticamente no PDF {pdf_path}. Usando Continente como fallback.")
                return ContinenteParser(pdf_path)
