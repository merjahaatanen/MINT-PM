"""
Parse Equipment All HTML file to extract equipment data.
This parses the Kendo UI grid data embedded in the HTML.
"""

import csv
import json
import re
from dataclasses import dataclass, asdict
from typing import List, Optional
from html.parser import HTMLParser


@dataclass
class EquipmentRecord:
    eq_id: str
    dept: str
    dept_num: str
    equipment_name: str
    asset_num: str
    make: str
    model: str
    vendor: str
    wo_qty: str
    modified: str


class EquipmentHTMLParser:
    """Parse the Equipment All HTML file and extract data."""
    
    def __init__(self, html_file_path: str):
        self.html_file_path = html_file_path
        self.records: List[EquipmentRecord] = []
    
    def parse(self) -> List[EquipmentRecord]:
        """Parse the HTML file and extract equipment records."""
        print(f"Parsing {self.html_file_path}...")
        
        with open(self.html_file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        print(f"HTML file size: {len(html_content):,} characters")
        
        # Find the gridEquipment div
        # The table rows are in the tbody section
        records = []
        
        # Method 1: Try to find all table rows with role="row" in the grid
        # Pattern for data rows in the Kendo Grid
        row_pattern = r'<tr[^>]*role=["\']row["\'][^>]*>(.*?)</tr>'
        rows = re.findall(row_pattern, html_content, re.DOTALL | re.IGNORECASE)
        
        print(f"Found {len(rows)} potential rows")
        
        for row in rows:
            # Extract cells from each row
            cell_pattern = r'<td[^>]*>(.*?)</td>'
            cells = re.findall(cell_pattern, row, re.DOTALL | re.IGNORECASE)
            
            if len(cells) >= 10:
                # Clean up cell content (remove HTML tags)
                cleaned_cells = [self._clean_cell(cell) for cell in cells[:10]]
                
                record = EquipmentRecord(
                    eq_id=cleaned_cells[0],
                    dept=cleaned_cells[1],
                    dept_num=cleaned_cells[2],
                    equipment_name=cleaned_cells[3],
                    asset_num=cleaned_cells[4],
                    make=cleaned_cells[5],
                    model=cleaned_cells[6],
                    vendor=cleaned_cells[7],
                    wo_qty=cleaned_cells[8],
                    modified=cleaned_cells[9]
                )
                records.append(record)
        
        # If no records found with method 1, try alternative parsing
        if not records:
            print("Trying alternative parsing method...")
            records = self._parse_alternative(html_content)
        
        self.records = records
        print(f"Successfully parsed {len(records)} records")
        return records
    
    def _clean_cell(self, cell_content: str) -> str:
        """Clean HTML tags and entities from cell content."""
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', cell_content)
        # Remove extra whitespace
        text = ' '.join(text.split())
        # Decode common HTML entities
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')
        return text.strip()
    
    def _parse_alternative(self, html_content: str) -> List[EquipmentRecord]:
        """Alternative parsing method using BeautifulSoup if available."""
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find the gridEquipment div
            grid = soup.find('div', {'id': 'gridEquipment'})
            if not grid:
                print("Could not find gridEquipment div")
                return []
            
            # Find the table body
            tbody = grid.find('tbody', {'role': 'rowgroup'})
            if not tbody:
                # Try any tbody
                tbody = grid.find('tbody')
            
            if not tbody:
                print("Could not find table body")
                return []
            
            rows = tbody.find_all('tr', {'role': 'row'})
            print(f"Found {len(rows)} rows with BeautifulSoup")
            
            records = []
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 10:
                    record = EquipmentRecord(
                        eq_id=cells[0].get_text(strip=True),
                        dept=cells[1].get_text(strip=True),
                        dept_num=cells[2].get_text(strip=True),
                        equipment_name=cells[3].get_text(strip=True),
                        asset_num=cells[4].get_text(strip=True),
                        make=cells[5].get_text(strip=True),
                        model=cells[6].get_text(strip=True),
                        vendor=cells[7].get_text(strip=True),
                        wo_qty=cells[8].get_text(strip=True),
                        modified=cells[9].get_text(strip=True)
                    )
                    records.append(record)
            
            return records
        except ImportError:
            print("BeautifulSoup not available, using regex parsing")
            return []
    
    def save_to_csv(self, filename: str = "equipment_data.csv"):
        """Save records to CSV file."""
        if not self.records:
            print("No records to save")
            return
        
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            fieldnames = asdict(self.records[0]).keys()
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for record in self.records:
                writer.writerow(asdict(record))
        
        print(f"Saved {len(self.records)} records to {filename}")
    
    def save_to_json(self, filename: str = "equipment_data.json"):
        """Save records to JSON file."""
        if not self.records:
            print("No records to save")
            return
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump([asdict(r) for r in self.records], f, indent=2)
        
        print(f"Saved {len(self.records)} records to {filename}")


def main():
    """Main function to parse HTML file."""
    import os
    
    # Default path
    html_file = r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper\Equipment All HTML"
    
    if not os.path.exists(html_file):
        print(f"HTML file not found: {html_file}")
        return
    
    parser = EquipmentHTMLParser(html_file)
    parser.parse()
    
    output_dir = r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper"
    
    csv_path = os.path.join(output_dir, "equipment_data.csv")
    json_path = os.path.join(output_dir, "equipment_data.json")
    
    parser.save_to_csv(csv_path)
    parser.save_to_json(json_path)
    
    # Print first few records as sample
    if parser.records:
        print("\nSample records:")
        for i, record in enumerate(parser.records[:5], 1):
            print(f"{i}. {record.eq_id} - {record.equipment_name} ({record.dept})")


if __name__ == "__main__":
    main()
