#!/usr/bin/env python3
"""
SL Veggies GRN Automation Script - Scheduler Version
Reads PDFs directly from a Google Drive folder, extracts via LlamaExtract,
and writes flattened rows to Google Sheets.
(No Gmail-to-Drive step — files are expected to already be in the Drive folder.)
"""

import os
import tempfile
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# LlamaExtract import
try:
    from llama_cloud_services import LlamaExtract
    LLAMA_AVAILABLE = True
except ImportError:
    LLAMA_AVAILABLE = False
    print("WARNING: llama_cloud_services not available")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('slveggies_automation.log'),
        logging.StreamHandler()
    ]
)

# ============================================================
# CONFIG — FILL IN THE PLACEHOLDERS BELOW BEFORE RUNNING
# ============================================================
CONFIG = {
    'pdf': {
        'llama_api_key': 'llx-Eqks2F5aRUmCv02ocmt15SxEdEgMyS83WL0Lr9u3mL20Cc4R',  # verify this is still your active key
        'llama_agent': 'Fatema GRN',            # <-- must match the agent name in LlamaCloud
        'drive_folder_id': '1F7uuAyh2PJlBzydUZRyRd0PtpgGp50R4',   # <-- folder containing SL Veggies PDFs
        'spreadsheet_id': '1DNowuKF1gk0AVu2Ytt1wKZqCtzllhPIZTAwk5p7aCNQ',     # <-- target Google Sheet
        'sheet_range': 'fatemagrn',                # tab name for extracted rows
        'days_back': 7,
        'max_files': 1000,
        'failed_extractions_sheet': 'failed_extractions'
    },
    'logs': {
        'spreadsheet_id': '1DNowuKF1gk0AVu2Ytt1wKZqCtzllhPIZTAwk5p7aCNQ',     # <-- usually same as above
        'sheet_name': 'workflow_logs_fatema',
        'remaining_sheet': 'remaining_files'
    }
}

# Canonical column order for the output sheet (matches the SL Veggies schema:
# billFrom / shipFrom / billDetails / lineItems / summary / description).
# Any unexpected keys returned by the extractor are appended after these.
PREFERRED_HEADERS = [
    # billDetails (flattened)
    'bill_no', 'bill_date', 'place_of_supply', 'po_number', 'po_date',
    # billFrom = supplier (prefixed)
    'bill_from_name', 'bill_from_address', 'bill_from_contact_no',
    'bill_from_gstin', 'bill_from_state',
    # shipFrom (prefixed)
    'ship_from_name', 'ship_from_address', 'ship_from_pin',
    'ship_from_state', 'ship_from_gstin', 'ship_from_pan',
    # lineItems fields (cgst/sgst nested objects flattened)
    'item_name', 'item_code', 'hsn_sac', 'fsn_no',
    'quantity', 'price_per_unit', 'taxable_amount',
    'cgst_amount', 'cgst_percentage', 'sgst_amount', 'sgst_percentage',
    'amount',
    # document-level summary (repeated on every row of the bill)
    'total_quantity', 'total_taxable_amount', 'total_cgst', 'total_sgst',
    'sub_total', 'grand_total', 'bill_description',
    # metadata
    'source_file', 'processed_date', 'drive_file_id'
]


class SLVeggiesAutomation:
    def __init__(self):
        self.drive_service = None
        self.sheets_service = None

        # API scopes (Gmail removed — Drive + Sheets only)
        self.drive_scopes = ['https://www.googleapis.com/auth/drive']
        self.sheets_scopes = ['https://www.googleapis.com/auth/spreadsheets']

        # Workflow logs
        self.workflow_logs = []

    def log(self, message: str, level: str = "INFO"):
        """Add log entry with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {
            "timestamp": timestamp,
            "level": level.upper(),
            "message": message
        }

        self.workflow_logs.append(log_entry)

        if level.upper() == "ERROR":
            logging.error(message)
        elif level.upper() == "WARNING":
            logging.warning(message)
        elif level.upper() == "SUCCESS":
            logging.info(f"SUCCESS: {message}")
        else:
            logging.info(message)

    def authenticate(self):
        """Authenticate using OAuth2 credentials file"""
        try:
            self.log("Starting authentication process...", "INFO")

            creds = None
            token_file = 'token.json'
            credentials_file = 'credentials.json'

            scopes = self.drive_scopes + self.sheets_scopes

            if os.path.exists(token_file):
                self.log("Loading cached credentials...", "INFO")
                creds = Credentials.from_authorized_user_file(token_file, scopes)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    self.log("Refreshing expired credentials...", "INFO")
                    creds.refresh(Request())
                else:
                    if not os.path.exists(credentials_file):
                        self.log("credentials.json not found. Please download it from Google Cloud Console", "ERROR")
                        return False

                    self.log("Starting OAuth flow...", "INFO")
                    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
                    creds = flow.run_local_server(port=0)

                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
                self.log(f"Credentials saved to {token_file}", "SUCCESS")

            self.drive_service = build('drive', 'v3', credentials=creds)
            self.sheets_service = build('sheets', 'v4', credentials=creds)

            self.log("Authentication successful!", "SUCCESS")
            return True

        except Exception as e:
            self.log(f"Authentication failed: {str(e)}", "ERROR")
            return False

    # ------------------------------------------------------------
    # Drive helpers
    # ------------------------------------------------------------
    def list_drive_pdfs(self, folder_id: str, days_back: int = 1, all_time: bool = False) -> List[Dict]:
        try:
            if all_time:
                query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
            else:
                start_datetime = datetime.utcnow() - timedelta(days=days_back - 1)
                start_str = start_datetime.strftime('%Y-%m-%dT00:00:00Z')
                query = (f"'{folder_id}' in parents and mimeType='application/pdf' "
                         f"and trashed=false and createdTime >= '{start_str}'")
            files = []
            page_token = None
            while True:
                results = self.drive_service.files().list(
                    q=query,
                    fields="nextPageToken, files(id, name, createdTime)",
                    orderBy="createdTime desc",
                    pageToken=page_token,
                    pageSize=100
                ).execute()
                files.extend(results.get('files', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            self.log(f"Found {len(files)} PDF files in folder ({'all time' if all_time else f'last {days_back} days'})", "INFO")
            return files
        except Exception as e:
            self.log(f"Failed to list PDFs: {str(e)}", "ERROR")
            return []

    def download_from_drive(self, file_id: str, file_name: str) -> bytes:
        try:
            self.log(f"Downloading: {file_name}", "INFO")
            request = self.drive_service.files().get_media(fileId=file_id)
            file_data = request.execute()
            self.log(f"Downloaded: {file_name}", "SUCCESS")
            return file_data
        except Exception as e:
            self.log(f"Failed to download {file_name}: {str(e)}", "ERROR")
            return b""

    # ------------------------------------------------------------
    # Sheets helpers
    # ------------------------------------------------------------
    def get_sheet_data(self, spreadsheet_id: str, sheet_name: str) -> List[List[str]]:
        """Get all data from the sheet"""
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=sheet_name,
                majorDimension="ROWS"
            ).execute()
            return result.get('values', [])
        except Exception as e:
            self.log(f"Failed to get sheet data: {str(e)}", "ERROR")
            return []

    def get_sheet_id(self, spreadsheet_id: str, sheet_name: str) -> int:
        """Get the numeric sheet ID for the given sheet name"""
        try:
            metadata = self.sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            for sheet in metadata.get('sheets', []):
                if sheet['properties']['title'] == sheet_name:
                    return sheet['properties']['sheetId']
            self.log(f"Sheet '{sheet_name}' not found", "ERROR")
            return 0
        except Exception as e:
            self.log(f"Failed to get sheet metadata: {str(e)}", "ERROR")
            return 0

    def get_existing_drive_ids(self, spreadsheet_id: str, sheet_range: str) -> set:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=sheet_range,
                majorDimension="ROWS"
            ).execute()
            values = result.get('values', [])
            if not values:
                return set()
            headers = values[0]
            if "drive_file_id" not in headers:
                self.log("No 'drive_file_id' column found in sheet", "WARNING")
                return set()
            id_index = headers.index("drive_file_id")
            existing_ids = {row[id_index] for row in values[1:] if len(row) > id_index and row[id_index]}
            self.log(f"Found {len(existing_ids)} existing file IDs in sheet", "INFO")
            return existing_ids
        except Exception as e:
            self.log(f"Failed to get existing file IDs: {str(e)}", "ERROR")
            return set()

    def _get_sheet_headers(self, spreadsheet_id: str, sheet_range: str) -> List[str]:
        try:
            sheet_name = sheet_range.split('!')[0]
            header_range = f"{sheet_name}!A1:AAA1"
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=header_range,
                majorDimension="ROWS"
            ).execute()
            values = result.get('values', [])
            headers = values[0] if values else []
            self.log(f"Fetched {len(headers)} existing headers from sheet", "INFO")
            return headers
        except Exception as e:
            self.log(f"Failed to get sheet headers: {str(e)}", "ERROR")
            return []

    def _update_sheet_headers(self, spreadsheet_id: str, sheet_range: str, new_headers: List[str]):
        try:
            sheet_name = sheet_range.split('!')[0]
            end_col = chr(64 + len(new_headers)) if len(new_headers) <= 26 else f"A{chr(64 + len(new_headers) - 26)}"
            header_range = f"{sheet_name}!A1:{end_col}1"
            body = {'values': [new_headers]}
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=header_range,
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            self.log(f"Updated sheet headers to {len(new_headers)} columns", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Failed to update sheet headers: {str(e)}", "ERROR")
            return False

    def _append_to_google_sheet(self, spreadsheet_id: str, range_name: str, values: List[List[Any]], max_retries: int = 3):
        """Append data to Google Sheet with retry mechanism"""
        for attempt in range(1, max_retries + 1):
            try:
                body = {'values': values}
                result = self.sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    valueInputOption='USER_ENTERED',
                    body=body
                ).execute()
                updated_cells = result.get('updates', {}).get('updatedCells', 0)
                self.log(f"Appended {updated_cells} cells to Google Sheet", "SUCCESS")
                return True
            except Exception as e:
                if attempt < max_retries:
                    self.log(f"Append attempt {attempt} failed: {str(e)}", "WARNING")
                    time.sleep(2)
                else:
                    self.log(f"Failed to append to Google Sheet after {max_retries} attempts: {str(e)}", "ERROR")
                    return False
        return False

    # ------------------------------------------------------------
    # Extraction validation (SL Veggies schema)
    # ------------------------------------------------------------
    def validate_extraction_quality(self, extracted_data: Dict, file_name: str) -> Dict:
        """
        Validate extraction quality against the SL Veggies schema
        (billFrom / shipFrom / billDetails / lineItems / summary).
        """
        issues = []

        items = extracted_data.get("lineItems") or []
        item_count = len(items)

        # Check 1: Minimum items threshold
        if item_count == 0:
            issues.append("No line items extracted")
            return {
                'is_valid': False,
                'item_count': 0,
                'issues': issues,
                'completeness_score': 0.0,
                'missing_required': 0,
                'missing_optional': 0
            }

        if item_count < 3:
            issues.append(f"Very low item count: {item_count}")

        # Check 2: Essential fields presence in line items
        # (itemCode/hsnSac/cgst/sgst are nullable per schema, so kept optional)
        required_fields = ['itemName', 'quantity']
        optional_fields = ['itemCode', 'hsnSac', 'fsnNo',
                           'pricePerUnit', 'taxableAmount', 'amount']

        missing_required = 0
        missing_optional = 0

        for idx, item in enumerate(items):
            for field in required_fields:
                value = item.get(field)
                if value is None or str(value).strip() == "":
                    missing_required += 1
                    if idx < 3:  # Log first 3 items only
                        issues.append(f"Item {idx+1} missing required field: {field}")

            for field in optional_fields:
                value = item.get(field)
                if value is None or str(value).strip() == "":
                    missing_optional += 1

        # Check 3: Header fields (billDetails + billFrom)
        bill = extracted_data.get('billDetails') or {}
        bill_from = extracted_data.get('billFrom') or {}
        missing_bill = [f for f in ['billNo', 'date', 'poNumber'] if not bill.get(f)]
        missing_bill_from = [f for f in ['name', 'gstin'] if not bill_from.get(f)]
        if missing_bill:
            issues.append(f"Missing billDetails fields: {', '.join(missing_bill)}")
        if missing_bill_from:
            issues.append(f"Missing billFrom fields: {', '.join(missing_bill_from)}")

        # Completeness score (avoid div-by-zero)
        total_required_checks = item_count * len(required_fields)
        total_optional_checks = item_count * len(optional_fields)

        required_score = 1.0 if total_required_checks == 0 else (total_required_checks - missing_required) / total_required_checks
        optional_score = 1.0 if total_optional_checks == 0 else (total_optional_checks - missing_optional) / total_optional_checks

        # Weight: 70% required, 30% optional
        completeness_score = (required_score * 0.7) + (optional_score * 0.3)

        # NOTE: item_count >= 3 kept from the Hyperpure script; if SL Veggies
        # bills often have 1-2 line items, lower this threshold to 1.
        is_valid = (
            item_count >= 3 and
            completeness_score >= 0.6 and
            missing_required < (item_count * 0.3)
        )

        return {
            'is_valid': is_valid,
            'item_count': item_count,
            'issues': issues,
            'completeness_score': completeness_score,
            'missing_required': missing_required,
            'missing_optional': missing_optional
        }

    def safe_extract_with_validation(self, agent, file_path: str, file_name: str,
                                     max_retries: int = 1) -> Dict:
        """Single extraction attempt with 7-second delay"""
        try:
            self.log("Extraction with 7-second delay...", "INFO")
            time.sleep(7)
            result = agent.extract(file_path)

            if result and result.data:
                validation = self.validate_extraction_quality(result.data, file_name)
                self.log(
                    f"Extracted {validation['item_count']} items, "
                    f"completeness: {validation['completeness_score']:.2%}",
                    "INFO"
                )

                if validation['is_valid'] or validation['completeness_score'] >= 0.4:
                    self.log(f"OK: Extraction successful ({validation['item_count']} items)", "SUCCESS")
                    return {
                        'success': True,
                        'result': result,
                        'attempts': 1,
                        'validation': validation,
                        'strategy_used': 'extended_delay'
                    }
                else:
                    self.log(f"WARN: Low quality extraction ({validation['completeness_score']:.2%})", "WARNING")
                    return {
                        'success': False,
                        'result': result,
                        'attempts': 1,
                        'validation': validation,
                        'strategy_used': 'extended_delay'
                    }
            else:
                self.log("FAIL: No data extracted", "ERROR")
                return {
                    'success': False,
                    'result': None,
                    'attempts': 1,
                    'validation': None,
                    'strategy_used': 'extended_delay'
                }

        except Exception as e:
            self.log(f"FAIL: Extraction failed - {str(e)}", "ERROR")
            return {
                'success': False,
                'result': None,
                'attempts': 1,
                'validation': None,
                'strategy_used': 'failed'
            }

    # ------------------------------------------------------------
    # JSON -> rows (SL Veggies schema)
    # ------------------------------------------------------------
    def process_extracted_data(self, extracted_data: Dict, file_info: Dict) -> List[Dict]:
        """
        Flatten the SL Veggies extraction JSON:
          - one output row per lineItems entry
          - billDetails / billFrom / shipFrom / summary / description repeated
            on every row (prefixed for clarity)
          - nested cgst/sgst objects flattened to *_amount / *_percentage
          - metadata columns (source_file, processed_date, drive_file_id)
        """
        rows = []

        items = extracted_data.get("lineItems") or []
        if not items:
            self.log(f"No 'lineItems' key found in {file_info['name']}", "WARNING")
            return rows

        bill = extracted_data.get("billDetails") or {}
        bill_from = extracted_data.get("billFrom") or {}
        ship_from = extracted_data.get("shipFrom") or {}
        summary = extracted_data.get("summary") or {}

        def nz(value):
            """None -> '' (schema allows nulls for several fields)"""
            return "" if value is None else value

        header_data = {
            # billDetails
            "bill_no": nz(bill.get("billNo")),
            "bill_date": nz(bill.get("date")),
            "place_of_supply": nz(bill.get("placeOfSupply")),
            "po_number": nz(bill.get("poNumber")),
            "po_date": nz(bill.get("poDate")),
            # billFrom (supplier)
            "bill_from_name": nz(bill_from.get("name")),
            "bill_from_address": nz(bill_from.get("address")),
            "bill_from_contact_no": nz(bill_from.get("contactNo")),
            "bill_from_gstin": nz(bill_from.get("gstin")),
            "bill_from_state": nz(bill_from.get("state")),
            # shipFrom
            "ship_from_name": nz(ship_from.get("name")),
            "ship_from_address": nz(ship_from.get("address")),
            "ship_from_pin": nz(ship_from.get("pin")),
            "ship_from_state": nz(ship_from.get("state")),
            "ship_from_gstin": nz(ship_from.get("gstin")),
            "ship_from_pan": nz(ship_from.get("pan")),
            # document-level summary (repeated on every row of this bill)
            "total_quantity": nz(summary.get("totalQuantity")),
            "total_taxable_amount": nz(summary.get("totalTaxableAmount")),
            "total_cgst": nz(summary.get("totalCgst")),
            "total_sgst": nz(summary.get("totalSgst")),
            "sub_total": nz(summary.get("subTotal")),
            "grand_total": nz(summary.get("total")),
            # document description/remarks (e.g. 'LOT RETURN')
            "bill_description": nz(extracted_data.get("description")),
        }

        processed_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for item in items:
            row = {}
            row.update(header_data)

            # line item fields (itemCode / hsnSac can legitimately be null)
            row["item_name"] = nz(item.get("itemName"))
            row["item_code"] = nz(item.get("itemCode"))
            row["hsn_sac"] = nz(item.get("hsnSac"))
            row["fsn_no"] = nz(item.get("fsnNo"))
            row["quantity"] = nz(item.get("quantity"))
            row["price_per_unit"] = nz(item.get("pricePerUnit"))
            row["taxable_amount"] = nz(item.get("taxableAmount"))
            row["amount"] = nz(item.get("amount"))

            # nested tax objects (whole object can be null per schema)
            cgst = item.get("cgst") or {}
            sgst = item.get("sgst") or {}
            row["cgst_amount"] = nz(cgst.get("amount"))
            row["cgst_percentage"] = nz(cgst.get("percentage"))
            row["sgst_amount"] = nz(sgst.get("amount"))
            row["sgst_percentage"] = nz(sgst.get("percentage"))

            # metadata
            row["source_file"] = file_info['name']
            row["processed_date"] = processed_date
            row["drive_file_id"] = file_info['id']

            # Note: 0 is a valid quantity/amount, so only drop None/"" (unlike
            # the Hyperpure script which dropped both via `not in`)
            cleaned_row = {k: v for k, v in row.items() if v is not None and v != ""}
            rows.append(cleaned_row)

        return rows

    def _ordered_headers(self, row_keys: List[str], existing_headers: List[str]) -> List[str]:
        """Build header list: existing order preserved, new keys added in
        PREFERRED_HEADERS order first, any leftovers appended at the end."""
        headers = list(existing_headers)
        for h in PREFERRED_HEADERS:
            if h in row_keys and h not in headers:
                headers.append(h)
        for k in row_keys:
            if k not in headers:
                headers.append(k)
        return headers

    # ------------------------------------------------------------
    # Failure reporting
    # ------------------------------------------------------------
    def save_failed_extractions(self, spreadsheet_id: str, sheet_name: str, failed_files: List[Dict]):
        """Save failed/incomplete extraction details to a dedicated sheet"""
        try:
            self.sheets_service.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range=sheet_name,
                body={}
            ).execute()

            headers = [[
                'Timestamp', 'File Name', 'File ID', 'Status',
                'Items Extracted', 'Completeness Score', 'Issues',
                'Attempts', 'Strategy Used'
            ]]
            self._append_to_google_sheet(spreadsheet_id, sheet_name, headers)

            data = []
            for f in failed_files:
                data.append([
                    f.get('timestamp', ''),
                    f.get('file_name', ''),
                    f.get('file_id', ''),
                    f.get('status', ''),
                    f.get('items_extracted', 0),
                    f"{f.get('completeness_score', 0):.2%}",
                    '; '.join(f.get('issues', [])),
                    f.get('attempts', 0),
                    f.get('strategy_used', '')
                ])

            if data:
                success = self._append_to_google_sheet(spreadsheet_id, sheet_name, data)
                if success:
                    self.log(f"Saved {len(failed_files)} failed/incomplete extractions to {sheet_name}", "INFO")
                    return True
            return False

        except Exception as e:
            self.log(f"Failed to save failed extractions: {str(e)}", "ERROR")
            return False

    # ------------------------------------------------------------
    # Main Drive -> Sheet workflow
    # ------------------------------------------------------------
    def process_pdf_workflow_enhanced(self, config: dict, skip_existing: bool = True):
        """Enhanced PDF workflow with validation"""
        if not LLAMA_AVAILABLE:
            self.log("LlamaExtract not available", "ERROR")
            return {'success': False, 'processed': 0, 'rows_added': 0, 'failed': 0, 'incomplete': 0}

        try:
            self.log("Starting Drive to Sheet workflow (SL Veggies)", "INFO")
            os.environ["LLAMA_CLOUD_API_KEY"] = config['llama_api_key']
            extractor = LlamaExtract()
            agent = extractor.get_agent(name=config['llama_agent'])

            if agent is None:
                self.log(f"Could not find agent '{config['llama_agent']}'", "ERROR")
                return {'success': False, 'processed': 0, 'rows_added': 0, 'failed': 0, 'incomplete': 0}

            self.log("LlamaExtract agent found", "SUCCESS")

            sheet_name = config['sheet_range'].split('!')[0]
            sheet_id = self.get_sheet_id(config['spreadsheet_id'], sheet_name)

            existing_ids = set()
            if skip_existing:
                existing_ids = self.get_existing_drive_ids(config['spreadsheet_id'], config['sheet_range'])
                self.log(f"Found {len(existing_ids)} files already in sheet", "INFO")

            pdf_files = self.list_drive_pdfs(config['drive_folder_id'], config['days_back'])
            self.log(f"Found {len(pdf_files)} total PDFs in Drive", "INFO")

            if skip_existing:
                original_count = len(pdf_files)
                pdf_files = [f for f in pdf_files if f['id'] not in existing_ids]
                self.log(f"Filtered out {original_count - len(pdf_files)} existing files", "INFO")
                self.log(f"Remaining PDFs to process: {len(pdf_files)}", "INFO")

            max_files = config.get('max_files', 500)
            if len(pdf_files) > max_files:
                self.log(f"Limiting to {max_files} files (increase 'max_files' in CONFIG if needed)", "WARNING")
                pdf_files = pdf_files[:max_files]

            if not pdf_files:
                self.log("No PDF files found", "WARNING")
                return {'success': True, 'processed': 0, 'rows_added': 0, 'failed': 0, 'incomplete': 0}

            self.log(f"Found {len(pdf_files)} PDF files. Processing with validation...", "INFO")

            existing_headers = self._get_sheet_headers(config['spreadsheet_id'], config['sheet_range'])

            processed_count = 0
            rows_added = 0
            failed_count = 0
            incomplete_extractions = []

            for i, file in enumerate(pdf_files):
                self.log(f"\n{'='*60}", "INFO")
                self.log(f"Processing PDF {i+1}/{len(pdf_files)}: {file['name']}", "INFO")
                self.log(f"{'='*60}", "INFO")

                pdf_data = self.download_from_drive(file['id'], file['name'])
                if not pdf_data:
                    failed_count += 1
                    incomplete_extractions.append({
                        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        'file_name': file['name'],
                        'file_id': file['id'],
                        'status': 'Download Failed',
                        'items_extracted': 0,
                        'completeness_score': 0,
                        'issues': ['Failed to download from Drive'],
                        'attempts': 0,
                        'strategy_used': 'N/A'
                    })
                    continue

                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
                        temp_file.write(pdf_data)
                        temp_path = temp_file.name

                    extraction_result = self.safe_extract_with_validation(
                        agent, temp_path, file['name'], max_retries=5
                    )

                    if not extraction_result['result']:
                        failed_count += 1
                        incomplete_extractions.append({
                            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'file_name': file['name'],
                            'file_id': file['id'],
                            'status': 'Extraction Failed',
                            'items_extracted': 0,
                            'completeness_score': 0,
                            'issues': ['All extraction attempts failed'],
                            'attempts': extraction_result['attempts'],
                            'strategy_used': extraction_result['strategy_used']
                        })
                        continue

                    extracted_data = extraction_result['result'].data
                    validation = extraction_result['validation']

                    rows = self.process_extracted_data(extracted_data, file)

                    if not rows:
                        self.log(f"No rows extracted from: {file['name']}", "WARNING")
                        failed_count += 1
                        incomplete_extractions.append({
                            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'file_name': file['name'],
                            'file_id': file['id'],
                            'status': 'No Rows Extracted',
                            'items_extracted': 0,
                            'completeness_score': 0,
                            'issues': ['No rows after processing'],
                            'attempts': extraction_result['attempts'],
                            'strategy_used': extraction_result['strategy_used']
                        })
                        continue

                    if validation['is_valid']:
                        self.log(
                            f"[OK] Quality extraction: {len(rows)} items, "
                            f"{validation['completeness_score']:.2%} complete",
                            "SUCCESS"
                        )
                    else:
                        self.log(
                            f"[WARN] Partial extraction: {len(rows)} items, "
                            f"{validation['completeness_score']:.2%} complete",
                            "WARNING"
                        )
                        incomplete_extractions.append({
                            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'file_name': file['name'],
                            'file_id': file['id'],
                            'status': 'Partial Extraction',
                            'items_extracted': len(rows),
                            'completeness_score': validation['completeness_score'],
                            'issues': validation['issues'],
                            'attempts': extraction_result['attempts'],
                            'strategy_used': extraction_result['strategy_used']
                        })

                    processed_count += 1

                    # Update sheet headers if new keys appeared
                    all_keys = list(set().union(*(row.keys() for row in rows)))
                    new_headers = self._ordered_headers(all_keys, existing_headers)
                    if len(new_headers) > len(existing_headers):
                        self._update_sheet_headers(config['spreadsheet_id'], config['sheet_range'], new_headers)
                        existing_headers = new_headers

                    values = [[row.get(h, "") for h in existing_headers] for row in rows]
                    success = self.replace_rows_for_file(
                        spreadsheet_id=config['spreadsheet_id'],
                        sheet_name=sheet_name,
                        file_id=file['id'],
                        headers=existing_headers,
                        new_rows=values,
                        sheet_id=sheet_id
                    )

                    if success:
                        rows_added += len(rows)
                        self.log(f"[OK] Saved {len(rows)} rows to sheet", "SUCCESS")
                    else:
                        self.log("[FAIL] Failed to save rows to sheet", "ERROR")
                        failed_count += 1

                finally:
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)

            if incomplete_extractions:
                failed_sheet = config.get('failed_extractions_sheet', 'failed_extractions')
                self.save_failed_extractions(
                    config['spreadsheet_id'],
                    failed_sheet,
                    incomplete_extractions
                )

            self.log(f"\n{'='*60}", "INFO")
            self.log("PDF Workflow Summary:", "INFO")
            self.log(f"  Total files: {len(pdf_files)}", "INFO")
            self.log(f"  [OK] Successfully processed: {processed_count}", "SUCCESS")
            self.log(f"  [OK] Rows added: {rows_added}", "SUCCESS")
            self.log(f"  [FAIL] Failed: {failed_count}", "ERROR" if failed_count > 0 else "INFO")
            self.log(f"  [WARN] Incomplete: {len(incomplete_extractions)}", "WARNING" if incomplete_extractions else "INFO")
            self.log(f"{'='*60}", "INFO")

            return {
                'success': True,
                'processed': processed_count,
                'rows_added': rows_added,
                'failed': failed_count,
                'incomplete': len(incomplete_extractions)
            }

        except Exception as e:
            self.log(f"PDF workflow failed: {str(e)}", "ERROR")
            return {'success': False, 'processed': 0, 'rows_added': 0, 'failed': 0, 'incomplete': 0}

    def replace_rows_for_file(self, spreadsheet_id: str, sheet_name: str, file_id: str,
                              headers: List[str], new_rows: List[List[Any]], sheet_id: int) -> bool:
        """Delete existing rows for the file if any, and append new rows"""
        try:
            values = self.get_sheet_data(spreadsheet_id, sheet_name)
            if not values:
                return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)

            current_headers = values[0]
            data_rows = values[1:]

            try:
                file_id_col = current_headers.index('drive_file_id')
            except ValueError:
                self.log("No 'drive_file_id' column found, appending new rows", "INFO")
                return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)

            rows_to_delete = []
            for idx, row in enumerate(data_rows, 2):
                if len(row) > file_id_col and row[file_id_col] == file_id:
                    rows_to_delete.append(idx)

            if rows_to_delete:
                rows_to_delete.sort(reverse=True)
                requests = []
                for row_idx in rows_to_delete:
                    requests.append({
                        'deleteDimension': {
                            'range': {
                                'sheetId': sheet_id,
                                'dimension': 'ROWS',
                                'startIndex': row_idx - 1,
                                'endIndex': row_idx
                            }
                        }
                    })
                body = {'requests': requests}
                self.sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body=body
                ).execute()
                self.log(f"Deleted {len(rows_to_delete)} existing rows for file {file_id}", "INFO")

            return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)
        except Exception as e:
            self.log(f"Failed to replace rows: {str(e)}", "ERROR")
            return False

    # ------------------------------------------------------------
    # Workflow logs
    # ------------------------------------------------------------
    def save_workflow_logs_to_sheet(self, workflow_name: str):
        """Save workflow logs to Google Sheets"""
        try:
            spreadsheet_id = CONFIG['logs']['spreadsheet_id']
            sheet_name = CONFIG['logs']['sheet_name']

            log_rows = []
            for log_entry in self.workflow_logs:
                log_rows.append([
                    log_entry['timestamp'],
                    workflow_name,
                    log_entry['level'],
                    log_entry['message']
                ])

            if not log_rows:
                self.log("No logs to save", "WARNING")
                return False

            existing_data = self.get_sheet_data(spreadsheet_id, sheet_name)
            if not existing_data or existing_data[0] != ['Timestamp', 'Workflow', 'Level', 'Message']:
                headers = [['Timestamp', 'Workflow', 'Level', 'Message']]
                self._append_to_google_sheet(spreadsheet_id, sheet_name, headers)
                self.log("Created workflow_logs sheet headers", "INFO")

            success = self._append_to_google_sheet(spreadsheet_id, sheet_name, log_rows)
            if success:
                logging.info(f"Saved {len(log_rows)} log entries to workflow_logs sheet")
                return True
            else:
                logging.error("Failed to save logs to sheet")
                return False
        except Exception as e:
            logging.error(f"Failed to save workflow logs: {str(e)}")
            return False

    def save_remaining_files(self, spreadsheet_id: str, sheet_name: str, files: List[Dict]):
        """Save list of remaining files to the specified sheet"""
        try:
            self.sheets_service.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range=sheet_name,
                body={}
            ).execute()
            self.log(f"Cleared existing data in {sheet_name}", "INFO")

            headers = [['File Name', 'File ID', 'Created Time']]
            self._append_to_google_sheet(spreadsheet_id, sheet_name, headers)

            data = [[f['name'], f['id'], f.get('createdTime', '')] for f in files]
            success = self._append_to_google_sheet(spreadsheet_id, sheet_name, data)
            if success:
                self.log(f"Saved {len(files)} remaining files to {sheet_name}", "SUCCESS")
                return True
            else:
                self.log(f"Failed to save remaining files to {sheet_name}", "ERROR")
                return False
        except Exception as e:
            self.log(f"Failed to save remaining files: {str(e)}", "ERROR")
            return False


def run_automation():
    """Main function to run the automation workflow"""
    print("=" * 80)
    print("SL Veggies GRN Automation - Scheduled Run")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    automation = SLVeggiesAutomation()

    # Authenticate
    print("\n[1/3] Authenticating with Google APIs...")
    if not automation.authenticate():
        print("Authentication failed. Exiting.")
        return

    print("[OK] Authentication successful")

    # Run Drive to Sheet workflow (no Gmail step)
    print("\n[2/3] Running Drive to Sheet workflow...")
    print("-" * 80)

    pdf_config = {
        'llama_api_key': CONFIG['pdf']['llama_api_key'],
        'llama_agent': CONFIG['pdf']['llama_agent'],
        'drive_folder_id': CONFIG['pdf']['drive_folder_id'],
        'spreadsheet_id': CONFIG['pdf']['spreadsheet_id'],
        'sheet_range': CONFIG['pdf']['sheet_range'],
        'days_back': CONFIG['pdf']['days_back'],
        'max_files': CONFIG['pdf']['max_files'],
        'failed_extractions_sheet': CONFIG['pdf']['failed_extractions_sheet']
    }

    pdf_result = automation.process_pdf_workflow_enhanced(pdf_config, skip_existing=True)

    # Log unique counts after workflow
    drive_files = automation.list_drive_pdfs(CONFIG['pdf']['drive_folder_id'], all_time=True)
    unique_drive = len(drive_files)
    existing_ids = automation.get_existing_drive_ids(CONFIG['pdf']['spreadsheet_id'], CONFIG['pdf']['sheet_range'])
    unique_sheet = len(existing_ids)
    automation.log(f"Unique files in Drive: {unique_drive}")
    automation.log(f"Unique files in Sheet: {unique_sheet}")

    # Handle remaining files if drive has more
    if unique_drive > unique_sheet:
        remaining_ids = set(f['id'] for f in drive_files) - existing_ids
        remaining_files = [f for f in drive_files if f['id'] in remaining_ids]
        automation.save_remaining_files(CONFIG['logs']['spreadsheet_id'], CONFIG['logs']['remaining_sheet'], remaining_files)

    # Save workflow logs
    print("\nSaving Drive to Sheet workflow logs to sheet...")
    automation.save_workflow_logs_to_sheet("Drive to Sheet")

    if pdf_result['success']:
        print(f"[OK] Drive to Sheet completed: {pdf_result['processed']} files processed, {pdf_result['rows_added']} rows added")
        if pdf_result['failed'] > 0:
            print(f"  [WARN] {pdf_result['failed']} files failed")
    else:
        print("[FAIL] Drive to Sheet workflow failed")

    # Summary
    print("\n[3/3] Workflow Summary")
    print("=" * 80)
    print(f"Drive to Sheet: {'[OK] Success' if pdf_result['success'] else '[FAIL] Failed'}")
    print(f"  - PDFs processed: {pdf_result['processed']}")
    print(f"  - Rows added: {pdf_result['rows_added']}")
    print(f"  - Failed: {pdf_result['failed']}")
    print(f"\nCompleted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)


if __name__ == "__main__":
    try:
        print("Starting scheduler to run every 3 hours...")
        while True:
            run_automation()
            print("\nWaiting 3 hours for next run...")
            time.sleep(3 * 3600)  # 3 hours
    except KeyboardInterrupt:
        print("\n\nScheduler interrupted by user")
    except Exception as e:
        logging.error(f"Fatal error: {str(e)}", exc_info=True)
        print(f"\nFatal error occurred: {str(e)}")
        print("Check slveggies_automation.log for details")
